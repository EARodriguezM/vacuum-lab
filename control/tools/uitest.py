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
from pathlib import Path
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.cdp import Chrome, Page, attach  # noqa: E402

PARTS = ("pages", "maths", "gating", "override", "prompt", "fault", "idle",
         "deploy", "form", "history", "signup", "start", "guided", "lost",
         "sending", "update", "guests")
SIZES = {"tablet": (1024, 600, False), "phone": (390, 844, True)}
ROLES = ("viewer", "operator", "admin", "tablet")
PASSWORD = "uitest-pass-1"
#: The smallest a button may be under a finger (styles.css, pointer: coarse).
TOUCH_PX = 40
#: The panel's poll period; the test waits in polls, not seconds.
POLL_S = 2.0

#: Loaded before the panel's own code in every page: counts the state polls,
#: keeps the requests that failed, keeps every AudioContext made, and counts
#: the tones played (`tones`).
INSTRUMENT = r"""
(() => {
  const ui = window.__ui = { polls: 0, failed: [], audio: [], tones: 0,
                             loaded: Date.now() };
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = String(input && input.url || input);
    const method = (init && init.method) || "GET";
    // The lab PC gone, on purpose: `ui.offline = true`. Not a failure.
    if (ui.offline && url.includes("/api/")) throw new TypeError("offline (uitest)");
    // A command held in flight until `ui.release()`: `ui.hold = true`; any
    // other request whose URL has `ui.holdPath` in it, the same way.
    if ((ui.hold && url.includes("/api/command"))
        || (ui.holdPath && url.includes(ui.holdPath))) {
      await new Promise((resolve) => { ui.release = resolve; });
    }
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
      createOscillator(...args) { ui.tones += 1; return super.createOscillator(...args); }
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
  // What sticks out, outside the boxes meant to scroll: named in the failure.
  const wide = sideways ? [...document.querySelectorAll("body *")]
    .filter((e) => !e.closest(".scroll-x, nav.tabs, .table-view"))
    .map((e) => [e, e.getBoundingClientRect()])
    .filter(([e, r]) => r.width > 0 && r.right > screenW + 1)
    .slice(-4).map(([e, r]) => `${e.tagName.toLowerCase()}.${String(e.className)
      .split(" ")[0]} to ${Math.round(r.right)}: ${(e.innerText || "")
      .trim().slice(0, 30)}`) : [];
  const coarse = matchMedia("(pointer: coarse)").matches;
  const small = coarse ? [...document.querySelectorAll("button, a.btn, select")]
    .filter((e) => e.offsetParent && !e.closest("[data-test-skip-size]"))
    .map((e) => [e, e.getBoundingClientRect()])
    .filter(([e, r]) => r.height > 0 && r.height < %(touch)s - 0.5)
    .map(([e, r]) => `${(e.dataset.test || (e.tagName === "SELECT"
      ? e.options[e.selectedIndex]?.text : e.innerText) || e.value || e.tagName)
      .trim().slice(0, 30)} ${Math.round(r.height)}px`) : [];
  const failed = window.__ui ? window.__ui.failed.splice(0) : [];
  return { bad, sideways, wide, small, failed };
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
            # Stopped before its shutter ever opened, its clean-up still owed.
            ("deposition", "aborted",
             {"base_mbar": 3.0e-5, "deposition_s": None, "shutter_closed": True,
              "working_mbar_setpoint": 5e-3, "cleanup_pending": True,
              "message": "the pump reported fault Err006"}),
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
    report.check(f"{label}: no sideways scroll", not found.get("sideways"),
                 "; ".join(found.get("wide") or [])[:200])
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
    report.check("with both it can", present(page, "override-confirm").get("enabled"),
                 str(page.js("(() => { const d = document.querySelector("
                             "\"[data-test='override-dialog']\"); return d ? {"
                             "reason: d.querySelector('input').value, checked: "
                             "d.querySelector('input[type=checkbox]').checked, "
                             "text: d.innerText.slice(0, 120), dialogs: "
                             "document.querySelectorAll('.overlay').length} : null; })()")))
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
        # Keep watching, for now.
        page.js("(document.querySelector(\"[data-test='reauth-password']\") || "
                "document.body).closest('form')?.querySelector('button[type=button]')"
                "?.click()")
        settle(page)
        # Answering an alarm stays open to an idle operator: it asks for the
        # password, and once typed the acknowledgement goes ahead.
        supervisor = server.app.supervisor
        raised = supervisor.raise_alarm("uitest_idle", "warning",
                                        "uitest: an alarm for an idle operator",
                                        "Raised by the browser test.")
        page.wait("document.querySelector(\"[data-test='alarm-ack']\")", timeout=10)
        ack = present(page, "alarm-ack")
        report.check("an idle operator's Acknowledge is not a dead button",
                     ack.get("shown") and ack.get("enabled"), str(ack))
        if asked:
            # Back to the normal limit: three seconds would idle the session
            # again before the panel's next poll could show it active.
            auth.idle_s = saved
            click(page, "alarm-ack")
            reopened = page.wait("document.querySelector(\"[data-test='reauth-password']\")",
                                 timeout=10)
            report.check("pressing it asks for the password", reopened)
            type_into(page, "reauth-password", PASSWORD)
            click(page, "reauth-submit")
            closed = page.wait("!document.querySelector(\"[data-test='reauth-password']\")",
                               timeout=10)
            acknowledged = _wait(lambda: not any(
                a["id"] == raised["id"] for a in supervisor.describe()["alarms"]), 10)
            report.check("and once it is typed, the alarm is acknowledged without "
                         "pressing again", acknowledged)
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

    # Saving a note keeps the run open; closing it goes back to the list.
    page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    settle(page)
    before = page.js("window.scrollY")
    click(page, row)
    page.wait("document.querySelector(\"[data-test='history-save']\")", timeout=10)
    # The page fills its form after its first frame: typed before that, the
    # note is overwritten (which the earlier version of this check missed).
    page.wait("!document.body.innerText.includes('Loading the curves')", timeout=10)
    settle(page)
    type_into(page, "history-notes", "uitest: a note, saved")
    click(page, "history-save")
    run_id = int(row.rsplit("-", 1)[1])
    saved = _wait(lambda: (server.app.db.run(run_id) or {}).get("notes")
                  == "uitest: a note, saved", 5)
    wait_polls(page)
    settle(page)
    still = present(page, "history-close").get("shown")
    kept = page.js("(document.querySelector(\"[data-test='history-notes']\") || {})"
                   ".value")
    report.check("saving a run's notes keeps the run open, with the note",
                 saved and still and kept == "uitest: a note, saved",
                 f"saved {saved}, still open {still}, box {kept!r}")
    click(page, "history-close")
    page.wait("document.querySelector(\"[data-test^='history-run-']\")", timeout=10)
    settle(page)
    back = page.js("window.scrollY")
    report.check("and closing it then goes back to the same place in the list",
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

    # A deposition whose shutter never opened is not "confirmed closed", and
    # its owed clean-up is an admin's to mark as done by hand.
    owed = next(r for r in server.app.db.runs(limit=50)
                if r["procedure"] == "deposition")
    open_as(page, server.base, sessions["operator"], f"history/{owed['id']}")
    page.wait("document.querySelector(\"[data-test='history-close']\")", timeout=10)
    page.wait("!document.body.innerText.includes('Loading the curves')", timeout=10)
    text = page.js("document.body.innerText")
    report.check("a shutter that never opened is said so, not 'confirmed'",
                 "never opened" in text and "Its clean-up is still owed" in text
                 and not present(page, "cleanup-checked-open").get("shown"),
                 text[text.find("Shutter"):][:40] if "Shutter" in text else "no row")
    admin = chrome.page(*SIZES["tablet"])
    open_as(admin, server.base, sessions["admin"], f"history/{owed['id']}")
    admin.wait("document.querySelector(\"[data-test='cleanup-checked-open']\")",
               timeout=10)
    click(admin, "cleanup-checked-open")
    admin.wait("document.querySelector(\"[data-test='cleanup-checked-reason']\")",
               timeout=10)
    shut = present(admin, "confirm-ok")
    type_into(admin, "cleanup-checked-reason", "uitest: the supply reads off")
    admin.js("document.querySelector(\"[data-test='cleanup-checked-tick']\").click()")
    settle(admin)
    audit(admin, report, "an owed clean-up marked as done by hand", shots)
    click(admin, "confirm-ok")
    settled = _wait(lambda: (server.app.db.run(owed["id"])["result"] or {})
                    .get("cleanup_pending") is False, 10)
    report.check("an admin marks it done by hand, with what was checked, and "
                 "only once both are given",
                 not shut.get("enabled") and settled
                 and (server.app.db.run(owed["id"])["result"].get("cleanup_checked")
                      or {}).get("by") == "adam",
                 f"confirm at first {shut}; settled {settled}")


def part_signup(chrome, server, sessions, report, shots):
    """Asking for an account from a phone: a Google sign-in proves the
    institutional account, then a password; the admin sees it verified."""
    from lab import google as googlemod
    from tools.fake_google import FakeGoogle
    google = FakeGoogle()
    google.interval = 1
    urls = (googlemod.DEVICE_URL, googlemod.TOKEN_URL)
    googlemod.DEVICE_URL, googlemod.TOKEN_URL = google.device_url, google.token_url
    path = os.path.join(server.folder, googlemod.SETTINGS_FILE)
    googlemod.save_settings(path, google.settings())
    try:
        page = chrome.page(*SIZES["phone"])
        open_as(page, server.base, None, "dashboard")
        page.wait("document.querySelector(\"[data-test='signin-mode-signup']\")")
        click(page, "signin-mode-signup")
        page.wait("document.querySelector(\"[data-test='signup-password']\")")
        typed = page.js("!!document.querySelector(\"[data-test='signup-username']\")")
        report.check("with Google set up, nobody types a username: the password "
                     "and note first, then Google", not typed)
        audit(page, report, "sign-up: password first", shots)
        type_into(page, "signup-password", "nina-pass-1")
        type_into(page, "signup-repeat", "nina-pass-1")
        type_into(page, "signup-note", "uitest")
        settle(page)
        click(page, "signup-submit")
        code = (page.wait("(document.querySelector(\"[data-test='signup-code']\")"
                          " || {}).innerText", timeout=15) or "").strip()
        report.check("a code to type at google.com/device is shown",
                     code and "google.com/device" in page.js("document.body.innerText"),
                     code)
        audit(page, report, "sign-up: the code", shots)
        if code:
            google.approve(code, "nina.ruiz@unal.edu.co", "Nina Ruiz")
        done = page.wait("document.body.innerText.includes('Request sent, as Nina Ruiz')",
                         timeout=20)
        user = server.app.db.user_by_name("nina.ruiz") or {}
        report.check("once the person accepts at Google, the request is made by "
                     "itself, for the account Google proved",
                     done and user.get("email_verified_at")
                     and user.get("display_name") == "Nina Ruiz"
                     and user.get("note") == "uitest",
                     f"page {bool(done)}, stored {user.get('username')}")
        audit(page, report, "sign-up: sent", shots)
        admin = chrome.page(*SIZES["tablet"])
        open_as(admin, server.base, sessions["admin"], "settings/people")
        listed = admin.wait("(document.querySelector(\"[data-test='address-nina.ruiz']\")"
                            " || {}).innerText", timeout=15) or ""
        report.check("the People page shows the request verified with Google",
                     "verified with Google" in listed, listed)
        audit(admin, report, "People with a verified request", shots)
    finally:
        if os.path.exists(path):
            os.unlink(path)
        googlemod.DEVICE_URL, googlemod.TOKEN_URL = urls
        google.close()


def part_start(chrome, server, sessions, report, shots):
    """Start repeats the recipe in a dialog; only its confirm starts the run."""
    engine = server.app.engine
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "tests")
    page.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
    if click(page, "run-new"):
        page.wait("document.querySelector(\"[data-test='run-start']\")")
    click(page, "procedure-hold")
    page.wait("[...document.querySelectorAll(\"input[data-test^='recipe-field-']\")]"
              ".every(e => e.value !== '')", timeout=10)
    settle(page)
    click(page, "run-start")
    shown = page.wait("document.querySelector(\"[data-test='start-dialog']\")"
                      " && document.querySelector(\"[data-test='start-dialog']\").innerText",
                      timeout=10)
    report.check("Start shows the recipe before anything runs",
                 shown and "Hold" in shown and not engine.status()["running"],
                 (shown or "")[:90])
    audit(page, report, "start dialog", shots)
    click(page, "confirm-cancel")
    settle(page)
    report.check("cancelling starts nothing",
                 not present(page, "start-dialog").get("shown")
                 and not engine.status()["running"])
    click(page, "run-start")
    page.wait("document.querySelector(\"[data-test='start-dialog']\")", timeout=10)
    click(page, "confirm-ok")
    going = _wait(lambda: engine.status()["running"], 10)
    report.check("its confirm starts the run, sent with confirm",
                 going and engine.status().get("procedure") == "hold",
                 str(engine.status().get("procedure")))
    if going:
        stopped = page.wait("document.querySelector(\"[data-test='run-stop']\")",
                            timeout=10)
        report.check("and the page follows it", stopped)
        # Stop is on the Dashboard too, through the same choice.
        open_as(page, server.base, sessions["operator"], "dashboard")
        page.wait("document.querySelector(\"[data-test='dash-stop']\")", timeout=10)
        click(page, "dash-stop")
        page.wait("document.querySelector(\"[data-test='choice-keep']\")", timeout=10)
        click(page, "choice-keep")
        ended = _wait(lambda: not engine.status()["running"], 15)
        report.check("Stop on the Dashboard ends the run, keeping the vacuum", ended)
        if not ended:
            engine.stop("uitest")
            _wait(lambda: not engine.status()["running"], 15)
    open_as(page, server.base, sessions["viewer"], "overview")
    moved = page.js("location.hash")
    report.check("an old link to the Overview lands on the Dashboard",
                 moved == "#dashboard", moved)


def part_guided(chrome, server, sessions, report, shots):
    """A guided step is answered from any page; what an ending leaves by hand
    stays on every page, sounding, until confirmed."""
    engine = server.app.engine
    # An alarm left by an earlier part would rightly take the tab's title.
    server.app.supervisor.acknowledge("all")
    gate = engine.hardware.gate
    driver, gate.driver = gate.driver, None      # by hand, as on the rig today
    code, body = server.call("/api/run/start", {
        "procedure": "rate_of_rise", "confirm": True,
        "config": {"mode": "guided", "settle_s": 0, "duration_s": 30,
                   "interval_s": 1, "volume_l": 10}}, sessions["operator"]["person"])
    try:
        if not report.check("a guided run starts", code == 200,
                            str(body.get("error", ""))[:100]):
            return
        page = chrome.page(*SIZES["tablet"])
        open_as(page, server.base, sessions["operator"], "dashboard")
        shown = page.wait("document.querySelector(\"[data-test='prompt']\")"
                          " && document.querySelector(\"[data-test='prompt']\").innerText",
                          timeout=15)
        report.check("the step it waits for is on the Dashboard too", shown,
                     (shown or "")[:80])
        title = page.js("document.title")
        report.check("and in the tab's title", "Waiting for you" in title, title)
        audit(page, report, "guided prompt on the Dashboard", shots)
        click(page, "prompt-done")
        gone = page.wait("!document.querySelector(\"[data-test='prompt']\")",
                         timeout=10)
        report.check("Done there carries the run on",
                     gone and engine.status().get("phase") in ("settle", "measuring",
                                                               "done"),
                     str(engine.status().get("phase")))
    finally:
        engine.stop("uitest")
        _wait(lambda: not engine.status()["running"], 15)
        gate.driver = driver

    engine.leave_by_hand([{"actuator": "magnetron", "label": "Magnetron",
                           "instruction": "Switch the magnetron power supply OFF.",
                           "value": "0 W"}])
    try:
        viewer = chrome.page(*SIZES["phone"])
        open_as(viewer, server.base, sessions["viewer"], "history")
        listed = viewer.wait("document.querySelector(\"[data-test='checklist']\")"
                             " && document.querySelector(\"[data-test='checklist']\").innerText",
                             timeout=10)
        report.check("a step left by hand is on every page, even a viewer's",
                     listed and "magnetron" in listed.lower()
                     and not present(viewer, "checklist-done").get("shown"),
                     (listed or "")[:80])
        audit(viewer, report, "checklist, viewer on a phone", shots)
        page = chrome.page(*SIZES["tablet"])
        open_as(page, server.base, sessions["operator"], "equipment/gauge")
        page.wait("document.querySelector(\"[data-test='checklist-done']\")", timeout=10)
        # Sound is unlocked by a first touch; before it, nothing sounds.
        before = present(page, "silence")
        page.js("window.dispatchEvent(new PointerEvent('pointerdown'))")
        wait_polls(page)
        quiet = present(page, "silence")
        click(page, "silence")
        settle(page)
        report.check("an operator may silence it here, once it sounds; it stays "
                     "listed", not before.get("shown") and quiet.get("shown")
                     and not present(page, "silence").get("shown")
                     and present(page, "checklist-done").get("shown"),
                     f"before a touch {before}, after {quiet}")
        started = server.call("/api/run/start", {
            "procedure": "hold", "confirm": True,
            "config": {"duration_min": 5, "interval_s": 1}},
            sessions["operator"]["person"])[1]
        report.check("no run starts meanwhile", not started.get("ok"),
                     str(started.get("error", "it started"))[:60])
        click(page, "checklist-done")
        done = page.wait("!document.querySelector(\"[data-test='checklist']\")",
                         timeout=10)
        report.check("Done takes it off every page", done and not engine.checklist())
    finally:
        for step in engine.checklist():
            engine.confirm_step(step["id"])
        if engine.status()["running"]:
            engine.stop("uitest")
            _wait(lambda: not engine.status()["running"], 15)


def part_lost(chrome, server, sessions, report, shots):
    """No answer from the lab PC: said, counted, the old figures greyed out --
    and a dialog open at that moment stays where it is."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "tests")
    page.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
    if click(page, "run-new"):
        page.wait("document.querySelector(\"[data-test='run-start']\")")
    click(page, "procedure-hold")
    page.wait("[...document.querySelectorAll(\"input[data-test^='recipe-field-']\")]"
              ".every(e => e.value !== '')", timeout=10)
    page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    click(page, "run-start")
    page.wait("document.querySelector(\"[data-test='start-dialog']\")", timeout=10)
    page.js("window.__ui.offline = true")
    said = page.wait("(document.querySelector(\"[data-test='lost-contact']\") || {})"
                     ".innerText", timeout=4 * POLL_S + 6)
    grey = page.js("!!document.querySelector('.wrap-inner.stale')")
    report.check("after two polls without an answer the panel says so, and greys "
                 "the old figures", said and "No contact with the lab PC since" in said
                 and grey, f"{said!r}, grey {grey}")
    box = page.js("(() => { const r = document.querySelector('.dialog')"
                  ".getBoundingClientRect(); return {top: r.top, bottom: r.bottom,"
                  " height: innerHeight}; })()") or {}
    report.check("a dialog open then stays on the screen",
                 box.get("top", -1) >= 0 and box.get("bottom", 1e9) <= box.get("height", 0),
                 str(box))
    time.sleep(2 * POLL_S + 1.5)
    later = page.js("(document.querySelector(\"[data-test='lost-contact']\") || {})"
                    ".innerText")
    report.check("and keeps counting how long it has been", later and later != said,
                 f"{said!r} then {later!r}")
    page.js("window.__ui.offline = false")
    back = page.wait("!document.querySelector(\"[data-test='lost-contact']\")"
                     " && !document.querySelector('.wrap-inner.stale')",
                     timeout=2 * POLL_S + 6)
    report.check("and goes back to normal at the next answer", back)
    click(page, "confirm-cancel")

    # On a watching tablet the beep may be silenced by anyone there: nobody
    # can sign in while the lab PC does not answer.
    kiosk = chrome.page(*SIZES["tablet"])
    open_as(kiosk, server.base, sessions["tablet"], "dashboard")
    kiosk.js("window.dispatchEvent(new PointerEvent('pointerdown'))")
    kiosk.js("window.__ui.offline = true")
    offered = kiosk.wait("(document.querySelector(\"[data-test='silence-lost']\") || {})"
                         ".innerText", timeout=4 * POLL_S + 6)
    click(kiosk, "silence-lost")
    settle(kiosk)
    quiet = not present(kiosk, "silence-lost").get("shown")
    still = present(kiosk, "lost-contact").get("shown")
    kiosk.js("window.__ui.offline = false")
    report.check("a watching tablet that loses the lab PC can be silenced by "
                 "anyone at it; the banner stays", offered and quiet and still,
                 f"offered {bool(offered)}, silenced {quiet}, banner {still}")

    # A poll slower than the control lease was saved before the cap: capped.
    slow = chrome.page(*SIZES["tablet"])
    open_as(slow, server.base, sessions["viewer"], "dashboard")
    slow.js("localStorage.setItem('vacuum-lab-settings', JSON.stringify({pollMs: 30000}))")
    slow.goto(f"{server.base}/?r={time.time()}#dashboard")
    slow.wait("window.__ui && window.__ui.polls >= 1", timeout=15)
    first = polls(slow)
    time.sleep(10.0 + POLL_S)
    report.check("a saved poll interval over 10 s is not used",
                 polls(slow) > first, f"{polls(slow) - first} polls in 12 s")


def part_sending(chrome, server, sessions, report, shots):
    """A command on its way cannot be cancelled: its answer comes back to the
    dialog that sent it."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "equipment/pump")
    page.wait("document.querySelector(\"[data-test='pump-on']\")", timeout=10)
    if not click(page, "pump-on") and not click(page, "pump-off"):
        report.check("a station button can be pressed", False)
        return
    page.wait("document.querySelector(\"[data-test='confirm-ok']\")", timeout=10)
    page.js("window.__ui.hold = true")
    click(page, "confirm-ok")
    page.wait("window.__ui.release", timeout=10)
    cancel = present(page, "confirm-cancel")
    report.check("while it is sending, Cancel is locked",
                 cancel.get("shown") and not cancel.get("enabled"), str(cancel))
    page.js("window.__ui.hold = false; window.__ui.release()")
    answered = page.wait("!document.querySelector(\"[data-test='confirm-ok']\")",
                         timeout=10)
    report.check("and the answer closes it, or shows the refusal in it", answered)


def part_update(chrome, server, sessions, report, shots):
    """A version the lab PC's updater made ready: shown under Settings ->
    Server, installed only by an admin, after a dialog that says the rig goes
    unwatched; then every page says so and new runs wait."""
    from lab import updates as updatesmod
    from tools import autoupdate

    folder = os.path.join(server.folder, "update")
    os.makedirs(folder)
    now = time.time()
    autoupdate.save_state(Path(folder), {
        "checked_at": now, "refused": {"v2026.10.26": "the tag is not signed"},
        "running": {"release": "20261020-0900-11111111", "tag": "v2026.10.20"},
        "latest": {"tag": "v2026.10.27", "release": "20261027-1200-abcdef12",
                   "commit": "c" * 40, "state": "ready", "title": "Version v2026.10.27",
                   "notes": "Install new versions from the panel.",
                   "tagged_at": now - 3600, "signed_by": "lab",
                   "tested_at": now - 600}})
    request = os.path.join(folder, updatesmod.REQUEST_FILE)
    engine = server.app.engine
    server.app.updates = updatesmod.Updates(folder, engine, server.app.db)
    try:
        page = chrome.page(*SIZES["phone"])
        open_as(page, server.base, sessions["operator"], "settings/server")
        page.wait("document.querySelector(\"[data-test='update-install']\")", timeout=10)
        offered = present(page, "update-install")
        text = page.js("document.querySelector(\"[data-test='update-card']\").innerText")
        report.check("an operator sees the version ready, its notes, and that an "
                     "admin installs it",
                     offered.get("shown") and not offered.get("enabled")
                     and "Install new versions" in (text or "")
                     and "An admin installs it" in (text or ""), str(offered))
        audit(page, report, "settings/server with a version ready (phone)", shots)

        server.call("/api/control/take", {"force": True}, sessions["admin"]["person"])
        admin = chrome.page(*SIZES["tablet"])
        open_as(admin, server.base, sessions["admin"], "settings/server")
        admin.wait("document.querySelector(\"[data-test='update-install']:not([disabled])\")",
                   timeout=10)
        marked = admin.js("!!document.querySelector(\"[data-test='subtab-server'] .badge\")")
        report.check("an admin sees the Server tab marked while a version is ready",
                     marked)
        audit(admin, report, "settings/server with a version ready (tablet)", shots)
        click(admin, "update-install")
        said = admin.wait("document.querySelector('.dialog') && "
                          "document.querySelector('.dialog').innerText", timeout=10)
        report.check("Install asks first: the rig goes unwatched, runs wait, it "
                     "goes back by itself",
                     said and "nothing watches" in said and "runs" in said
                     and "by itself" in said and not os.path.exists(request),
                     (said or "")[:80])
        click(admin, "confirm-ok")
        report.check("its confirm leaves the request for the installer",
                     _wait(lambda: os.path.exists(request), 10))
        # On the admin's page: the operator's is behind it now, and a hidden
        # page does not poll.
        banner = admin.wait("document.querySelector(\"[data-test='update-banner']\")"
                            " && document.querySelector(\"[data-test='update-banner']\")"
                            ".innerText", timeout=10)
        report.check("the page says an install is under way, and by whom",
                     banner and "v2026.10.27" in banner and "Adam" in banner,
                     (banner or "")[:80])
        refused = engine.start("hold", {})
        report.check("and no run starts meanwhile",
                     "being installed" in refused.get("error", ""),
                     refused.get("error", "")[:80])
    finally:
        server.app.updates = None
        engine.unhold()
        server.call("/api/control/release", {}, sessions["admin"]["person"])


def part_guests(chrome, server, sessions, report, shots):
    """Watching without an account: an admin allows it under Settings ->
    People, the sign-in page then offers it, and a guest sees every page and
    can press, download and hear nothing; a guest dropped by a restart joins
    again by itself, and switching guests off sends it back to signing in."""
    auth = server.app.auth
    owner = sessions["admin"]["person"]
    present_js = "!!document.querySelector(\"[data-test='%s']\")"
    admin = chrome.page(*SIZES["tablet"])
    open_as(admin, server.base, sessions["admin"], "settings/people")
    admin.wait(present_js % "guests-toggle", timeout=10)
    card = admin.js("document.querySelector(\"[data-test='guests-card']\").innerText")
    report.check("guests are off until an admin allows them",
                 "Not allowed" in (card or ""), (card or "")[:60])
    click(admin, "guests-toggle")
    said = admin.wait("document.querySelector('.dialog') && "
                      "document.querySelector('.dialog').innerText", timeout=10)
    report.check("allowing guests asks first, saying what they will see",
                 said and "activity log" in said and "cannot change" in said,
                 (said or "")[:80])
    click(admin, "confirm-ok")
    allowed = admin.wait("(document.querySelector(\"[data-test='guests-card']\")"
                         " || {}).innerText.includes('Allowed.')", timeout=10)
    report.check("... and then they may", allowed
                 and auth.status().get("guests") is True)
    audit(admin, report, "settings/people with guests allowed", shots)

    page = chrome.page(*SIZES["phone"])
    open_as(page, server.base, None, "dashboard")
    page.wait(present_js % "guest-watch", timeout=10)
    audit(page, report, "signed out, guests allowed", shots)
    click(page, "guest-watch")
    joined = page.wait(present_js % "guest-signin", timeout=10)
    chip = page.js("document.querySelector('header.top').innerText") or ""
    report.check("Watch as a guest opens the panel, watching, with Sign in "
                 "beside it", joined and "Guest" in chip, chip[:80])
    report.check("... and nothing that acts is offered: no control, no "
                 "Silence, no Take control",
                 not page.js(present_js % "silence")
                 and "Take control" not in chip)

    click(page, "tab-history")
    page.wait(present_js % "guest-notice", timeout=10)
    text = page.js("document.body.innerText") or ""
    report.check("History offers a guest no downloads, and says why "
                 "nothing can be pressed",
                 "All runs (CSV)" not in text and "SQLite file" not in text
                 and "watching as a guest" in text)

    # A restart drops every guest: the page joins again by itself.
    before = page.js("JSON.parse(localStorage.getItem('vacuum-lab-session')"
                     " || '{}').guest")
    with auth._lock:
        auth._guests.clear()
    again = page.wait(
        "(JSON.parse(localStorage.getItem('vacuum-lab-session') || '{}').guest"
        f" || '') !== {json.dumps(before)}"
        " && !!document.querySelector(\"[data-test='guest-signin']\")"
        " && !document.querySelector(\"[data-test='signin-submit']\")",
        timeout=20)
    report.check("a guest dropped by a restart joins again without the "
                 "sign-in page", again
                 and auth.guests_report()["watching"] >= 1)

    # A rejoin that hangs: after two polls without news the page is greyed
    # out, and comes back once the rejoin answers.
    time.sleep(10.5)                  # a rejoin is tried at most every 10 s
    page.js("window.__ui.holdPath = '/api/guest'")
    with auth._lock:
        auth._guests.clear()
    veiled = page.wait(present_js % "lost-contact", timeout=20)
    page.js("window.__ui.holdPath = null; window.__ui.release && window.__ui.release()")
    cleared = page.wait(f"!{present_js % 'lost-contact'}"
                        f" && {present_js % 'guest-signin'}", timeout=20)
    report.check("a rejoin that hangs greys the page out, and it comes back "
                 "when the rejoin answers", veiled and cleared,
                 f"veiled {bool(veiled)}, back {bool(cleared)}")

    # Sign in pressed just as the guest lapsed: the person still gets the
    # sign-in page, not a guest's page again.
    with auth._lock:
        auth._guests.clear()
    click(page, "guest-signin")
    back = page.wait(present_js % "signin-submit", timeout=10)
    time.sleep(3)
    stayed = (page.js(present_js % "signin-submit")
              and not page.js("JSON.parse(localStorage.getItem('vacuum-lab-session')"
                              " || '{}').guest"))
    report.check("Sign in on a guest that just lapsed reaches the sign-in page "
                 "and stays there", back and stayed)
    report.check("Sign in from a guest's page ends the guest and offers "
                 "every choice, watching again among them",
                 back and page.js(present_js % "guest-watch"))

    # A kiosk that lost its pairing is paired again, never left as a guest:
    # a guest's screen sounds no alarm. (`?pair` stands for the kiosk here.)
    kiosk = chrome.page(*SIZES["tablet"])
    kiosk.goto(f"{server.base}/?seed")
    kiosk.wait("document.readyState === 'complete'")
    kiosk.js("localStorage.clear()")
    kiosk.goto(f"{server.base}/?pair&r={time.time()}#dashboard")
    kiosk.wait(present_js % "signin-mode-pair", timeout=15)
    report.check("a kiosk's sign-in page offers pairing, never watching as a "
                 "guest", not kiosk.js(present_js % "guest-watch"))

    # Every page, as a guest, at both sizes: nothing refused on the way.
    for size in SIZES:
        code, body = server.call("/api/guest", {})
        report.guarded(f"the guest@{size} sweep", lambda s=size, b=body: sweep(
            chrome, server, "guest", s, {"guest": b.get("token")}, report, shots))

    # A critical alarm: an operator's screen sounds it, a guest's never does,
    # and says so on the Dashboard too.
    from tools import fake_tc600

    def touched(page):
        # A real press, which a browser counts as the gesture that allows
        # sound -- a script's event is not one.
        for kind in ("mousePressed", "mouseReleased"):
            page.call("Input.dispatchMouseEvent", type=kind, x=40, y=30,
                      button="left", clickCount=1)

    quiet = chrome.page(*SIZES["tablet"])
    open_as(quiet, server.base, {"guest": server.call("/api/guest", {})[1]
                                 .get("token")}, "dashboard")
    notice = quiet.wait("(document.querySelector(\"[data-test='guest-notice']\")"
                        " || {}).innerText", timeout=10) or ""
    report.check("a guest's Dashboard says it only watches and that alarms are "
                 "silent there", "silent" in notice, notice[:80])
    loud = chrome.page(*SIZES["tablet"])
    open_as(loud, server.base, sessions["operator"], "dashboard")
    touched(quiet)
    touched(loud)
    fake_tc600.STATE[303] = "Err006"
    try:
        alarm = ("(document.querySelector(\"[data-test='alarm-bar']\") || {})"
                 ".innerText || ''")
        shown = quiet.wait(f"({alarm}).includes('Err006')", timeout=20)
        loud.wait(f"({alarm}).includes('Err006')", timeout=20)
        time.sleep(6)
        heard = loud.js("window.__ui.tones")
        silent = quiet.js("window.__ui.tones")
        report.check("a critical alarm sounds on an operator's screen and never "
                     "on a guest's", shown and heard and silent == 0,
                     f"operator {heard} tones, guest {silent}")
    finally:
        fake_tc600.STATE[303] = "000000"
        _wait(lambda: not (server.app.supervisor.recovering or {}).get("active"), 30)
        server.call("/api/alarms/ack", {"id": "all"}, sessions["operator"]["person"])

    try:
        watcher = chrome.page(*SIZES["tablet"])
        open_as(watcher, server.base, {"guest": server.call("/api/guest", {})[1]
                                       .get("token")}, "dashboard")
        watcher.wait(present_js % "guest-signin", timeout=10)
        server.call("/api/users/guests", {"allowed": False}, owner)
        # The sign-in page first, then the server asked again whether guests
        # are allowed: the button goes with that answer.
        out = watcher.wait(f"{present_js % 'signin-submit'}"
                           f" && !{present_js % 'guest-watch'}", timeout=20)
        report.check("switching guests off sends a guest's page to the sign-in "
                     "page, which no longer offers watching", out)
    finally:
        server.call("/api/users/guests", {"allowed": False}, owner)


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
                  "history": part_history, "signup": part_signup,
                  "start": part_start, "guided": part_guided, "lost": part_lost,
                  "sending": part_sending, "update": part_update,
                  "guests": part_guests}


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
