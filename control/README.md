<h1 align="center">Vacuum Lab — control</h1>
<div align="center">

<i>The software that runs the rig: drivers for the TC 600 and the TPG 252 A, interlocks, a recipe engine, a SQLite record of every run and a web panel — on an offline Linux PC, with nothing to install but <code>pyserial</code>.</i>

<br><br>

<img src="https://img.shields.io/badge/python-3.11%E2%80%933.14-3776AB?logo=python&logoColor=white" alt="Python 3.11 to 3.14"/>
<img src="https://img.shields.io/badge/dependencies-pyserial%20only-2b9348" alt="Only pyserial"/>
<img src="https://img.shields.io/badge/self%20test-~590%20checks-2b9348" alt="About 590 checks"/>

</div>

---

> Part of [Vacuum Lab](../README.md). All paths and commands below are
> relative to this folder: `cd control` first.

Native Linux control and automation of a **magnetron sputter deposition rig**
— thin films grown in an argon plasma, on a chamber pumped by a Pfeiffer
Vacuum **TC 600** turbopump over RS-485 and measured by a Pfeiffer
**TPG 252 A** gauge controller over RS-232 — with a web panel, a recipe
engine, and a database of every run.

Pure Python. The only dependency is `pyserial`. The web UI has no build step:
Preact and htm are vendored as plain ES modules, because the lab PC is offline
and has no Node toolchain.

```
TC 600    → HXSP-485 → FTDI FT232R (/dev/tc600)   ┐
                                                  ├→ lab PC → webserver.py → browser
TPG 252 A → RS-232   → FTDI US232R (/dev/tpg252)  ┘
```

**Contents** — [Quick start](#quick-start) · [The web panel](#the-web-panel) · [The deposition process](#the-deposition-process) · [Runs and the database](#runs-and-the-database) · [Bringing the gauge link up](#bringing-the-gauge-link-up) · [Command line tools](#command-line-tools) · [Accessory connectors](#accessory-connectors) · [Verified working configuration](#verified-working-configuration) · [Deploying to the lab PC](#deploying-to-the-lab-pc) · [Releasing a new version](#releasing-a-new-version) · [Backups](#backups) · [The morning report](#the-morning-report) · [One-time setup: serial port permissions](#one-time-setup-serial-port-permissions) · [Wiring](#wiring) · [If nothing answers](#if-nothing-answers) · [Choosing an adapter](#choosing-an-adapter) · [Verifying the parameter table](#verifying-the-parameter-table) · [Testing without hardware](#testing-without-hardware) · [Layout](#layout) · [Protocol notes](#protocol-notes) · [Why not LabVIEW or PV TurboViewer](#why-not-labview-or-pv-turboviewer)

![The Dashboard during a simulated deposition](../docs/images/panel-dashboard.png)

---

## Quick start

No hardware at all — the whole rig simulated, including the argon, gate
valve, magnetron and shutter, so a complete deposition runs end to end:

```bash
./webserver.py --simulate --no-auth --db /tmp/lab.db
# -> http://localhost:8600/
```

`--no-auth` switches accounts off, which is fine on a laptop and never on the
rig. To try the sign-in pages in the simulator instead, leave it out and make
yourself the owner first: `./tools/users.py --db /tmp/lab.db owner me`.

A full simulated deposition takes about five minutes and exercises every
phase. Use it to rehearse a recipe, or to develop without going to the lab.

On the real rig — the ports are found automatically:

```bash
./tools/users.py owner YOURNAME          # once: the chamber's owner
./webserver.py --port 8600
# Looking for the instruments:
#   TC 600 turbopump found on /dev/tc600 (/dev/ttyUSB1): TC600, firmware 017500
#   TPG gauge controller found on /dev/tpg252 (/dev/ttyUSB0): firmware BG509727-C at 9600 baud
```

A port can still be given (`--serial /dev/ttyUSB1`): it is tried first, and
the other ports are searched only if nothing of the right kind answers there.
`--no-port-search` uses exactly the ports given.

`--no-pump` and `--no-gauge` start the panel without an instrument. That
matters during bring-up: a gauge that is not wired yet must not stop you
driving the pump. It does stop you starting the *turbopump* without an
override, though — with no gauge there is no way to tell whether the chamber
is roughed out, and that is the one question the crossover interlock exists
to answer.

---

## The web panel

Six tabs, responsive down to a phone, light and dark. Everybody signs in
first (see **Accounts** below).

| Tab | What it does |
|---|---|
| **Dashboard** | The wall display, read standing up. A **guide** says what the rig is doing in words, which step of the deposition route it is on, what ends that step and roughly when (countdowns to the shutter opening and closing, spin-up and pump-down estimates); four tiles for pressure, rotor, drive load and plasma/gas; a **vacuum scale** from atmosphere to UHV with the rig's landmarks on it; the run's curves with its phases and limits (or the last hour when none is recording); a **health checklist** and an **activity feed** with every run one click from its record. **Explain** adds a sentence to every figure for newcomers; **Full screen** is for a monitor on the lab wall |
| **Overview** | Plain-language state of the rig, live tiles, the active run, the last hour |
| **Equipment** | One sub-tab per instrument and per installed actuator. **TC 600 turbopump**: station and turbopump control, speed setpoint, faults, all four accessory connectors — and **pump trends**: up to 24 h of pressure, rotor speed, drive current, power and supply voltage on one time axis, health tiles, the load curve, and the pump temperature where one is measured. **TPG gauge controller**: both channels, gauge on/off, filter, unit, switching functions, error register, capability probe — and **pressure history** per channel with the reason for every gap, the rate of change, and window statistics. **Argon flow, gate valve, magnetron, shutter**: whether software drives it or a person does, what it is set to — and whether that was confirmed — and its recorded history |
| **Process** | Pick a recipe, fill in the form, watch it run on the full chart set, answer its prompts. A recipe that needs equipment not installed on the rig yet is listed but switched off, saying what it is waiting for |
| **History** | Every recorded run: browse, search, inspect, annotate, export, delete; the full chart set per run, comparison with earlier pump-downs, and **trends across runs** |
| **Settings** | **My account**; for admins **People** (approve sign-ups, roles, password resets) and **Tablets** (pairing); **Rig equipment** (what is physically installed — argon line, gate valve, magnetron, shutter, pump heater), instrument ports, controller address, poll rate, theme, database status |

### Reading the charts

Every chart has a **Table** button with the same numbers, and every stack of
charts shares one crosshair: hover any of them (or focus one and use the arrow
keys) to read all of them at that instant. A dashed line is always a limit
somebody wrote down — never a grid line — and the label says which.

| Chart | What it answers |
|---|---|
| **The stack** — pressure, rotor speed, drive current, drive power, supply voltage | What happened, and in what order. A gas overload reads as one event: the pressure steps up, and in the same second the rotor sags, the current and power rise to the drive's limit and the supply dips. The dashed lines are the pump's ratings from its manual: the **80 % switchpoint** (1200 Hz — held below it, the drive trips with Err006), the **gas-mode-0 power line** (60 W at full speed in argon, 80 W at ≤ 1300 Hz), the rated **24 V ± 5 %** supply band. A red strip under the power chart marks the drive's over-temperature flag |
| **Load curve** — drive current (or power) against chamber pressure | How much gas this pump can carry. At full speed the points rise with pressure along one curve; where that curve meets the drive's power limit the rotor can no longer hold speed and the points turn orange. That knee is this rig's real ceiling for working pressure with the gate open — measured, not looked up. Hovering a point shows when it was taken on the time charts |
| **Pump health tiles** | Last run-up time against the rated 2.5 min, friction power at full speed with nothing to pump (it creeps up as bearings wear), the lowest supply voltage under load, and how long the gas held the rotor below full speed |
| **Pump temperature** | Shown only when measured. Firmware 017500 has no electronics, motor, bearing or pump-bottom sensor, and the TMS reads 0 while its heater is off — the panel says so rather than drawing a pump at 0 °C. The heat going into the pump is the drive power |
| **Rate of change** | How fast the pressure is moving, \|d ln p/dt\| in %/min, rising and falling in different colours — the quantity the base-pressure judgement uses, with its threshold drawn, so you can see why a run called base when it did |
| **Working pressure** (deposition) | The argon pressure close up, against the setpoint, the ½×–2× window the backfill accepts and the abort ceiling, with the deviation in percent — the chart to watch while turning a needle valve |
| **Pump-down on log-log axes** | Pressure against time since the turbopump started from rest, with the local slope n of p ∝ t⁻ⁿ. **n ≈ 1** is water coming off the walls (bake it), **n ≈ 0.5** is gas diffusing out of seals, **n → 0** is a floor: a leak, permeation, or the pump's limit |
| **Compared with other pump-downs** | This run against earlier ones, aligned at the turbopump start — is the chamber behaving as it used to? |
| **Trends across runs** | One dot per run: base pressure, time to base, run-up time, friction power, lowest supply voltage, time held below full speed, leak rate. These answer what one run cannot: whether the chamber or the pump is getting worse |
| **Gauge strips** | Under each channel, why its line has a gap: under- or overrange, sensor off, no sensor, error |

The rolling history behind the pump and gauge pages is kept by the server
every 2 s whether or not a run is going, for 24 hours, in memory: a restart
starts it afresh. Runs are the durable record. Any window can be downloaded as
CSV from its card, and a run can be linked to directly as `#history/<id>`.

### When something goes wrong

**The forevacuum pump is never switched off automatically.** No abort, fault
or alarm stops the pumping station; only a person choosing *Power down* does.
Switching the station off stops the forevacuum pump: a rotor still coasting
loses its backing, and the chamber leaks back up through the stopped pumps,
so a fault that powered the rig down at night would leave it near
atmosphere by the morning. (This rig has no vent valve, so the TC 600's
automatic venting does not apply; on a pump with one, the factory setting
would also vent the chamber below 50 % speed.)

Instead, when the turbopump is in trouble the panel puts it in its **safe
state**: turbopump motor off (first, so the next step cannot restart it),
drive fault acknowledged, pumping station — the forevacuum pump — kept on.
The rotor coasts down backed by the forevacuum pump and the chamber stays
under rough vacuum, ready for a restart through the crossover interlock. This
happens:

- automatically after any **drive fault** (Err006 and the rest), whether or
  not a run is going — at most three times in 15 minutes, after which it
  alarms and leaves the fault for a person. *Faults and recovery* on the
  pump's Equipment page switches this off (admins), and so does
  `--no-auto-recovery`;
- when a run ends because the pump was in trouble: a drive fault,
  over-temperature, or the rotor dragged below 85 % of nominal speed (or below
  95 % for two minutes) by too much gas;
- when you press **Make safe now** on the pump's Equipment page.

**Alarms** appear at the top of every page and stay until acknowledged, even
once their cause has cleared, so a fault in the night is still there in the
morning. Each says what the panel already did about it. The browser tab's
title carries the worst one, and critical ones can sound (Settings). The
Overview tab keeps the log.

Besides drive faults and the rest, the panel raises an alarm when:

- the **turbopump was started without the panel asking** (from the drive's
  front panel, its remote plug, or a fault that cleared by itself) — critical
  if the chamber is not known to be roughed;
- the turbopump is **already running when the panel starts**, or is **running
  again after a silence** the panel did not watch (a drive restarts by itself
  when its power returns);
- a **run was cut off by a crash or a power cut**, or **ended by a restart** of
  the panel, with what was done about its clean-up;
- a **run's clean-up did not succeed** — the heater, the gas or the plasma may
  still be on. Check the rig by hand. The panel tries that clean-up again as
  soon as the pump answers — up to five times, a minute apart, and no run can
  start meanwhile — and at its next start if that fails, and says how it went;
- **database writes are failing** — the run's samples and log are being lost
  (check the disk or SD card); the run's safety checks carry on;
- **a run has stopped checking the rig**: its loop has not looked at the
  instruments for 5 s. Watch the rig yourself and stop the run if it does not
  recover.
- **the panel was restarted** (a warning) or **stopped unexpectedly** (critical:
  a crash or a power cut). Either way nothing watched the rig, recorded or
  raised alarms in between; the pump kept doing what it was last told. Check
  the pump and the chamber, then acknowledge. Alarms still waiting for an
  acknowledgement survive a restart, so a fault in the night is not lost with
  it, and a restart is a warning to acknowledge each time;
- **no backup** has been made, **the last backup failed**, or **the last good
  one is more than 48 hours old** (see *Backups*); the runs and the accounts
  then exist only on that machine;
- **the chart recorder has stopped**: the pump and gauge pages stop adding to
  their charts (runs still record);
- a red banner **"Nothing is raising alarms"**: the part of the panel that
  watches for faults has stopped, so no new alarm will appear. Watch the rig
  yourself and ask whoever looks after the panel to restart it; the journal
  (`journalctl -u tc600-web -p err`) says what went wrong.

One lost reply on the bus raises none of these: the panel believes a change in
the drive only when two readings in a row show it.

**An aborted run stays on the Process tab**, with its charts continuing live
in an *After the run* band — the rotor coasting, the pressure recovering —
until you press *Start a new run*.

**Err006** is the run-up time error: with the factory settings (PM 0504 BE,
section 4.1: run-up time 15 min, switchpoint 80 %), a rotor below 1200 Hz once
15 minutes have passed since the motor started trips the drive. On this rig
it has been caused by too much gas: the drive reaches its power limit, the
rotor sinks below the switchpoint, and the drive stops. *Faults and recovery*
reads the drive's own settings and its last ten faults, and lists what each
error code means.

### Finding the instruments automatically

You do not have to tell the panel which port each instrument is on. At
startup it asks every serial port what is on it — a *read* of the TC 600's
type, the TPG's firmware query; nothing is written — and opens each
instrument where it answers, by the most stable name that port has. A
replugged USB adapter (which renumbers `/dev/ttyUSB0` and `/dev/ttyUSB1`), a
replaced adapter, or a different USB socket does not matter. If an
instrument stops answering for 10 s, the panel looks for it on the other free
ports about once a minute and reconnects by itself, with an alarm saying where
it went. **Settings → Finding the instruments** shows each instrument's port,
lets you search now, and *Detect instruments now* lists what answers on every
port. `--no-port-search` pins the ports instead.

### Choosing which port an instrument is on

Settings has a port picker: filter by **USB adapters** or **onboard serial**,
see what is actually attached, and move an instrument to a different port
without touching a command line. It is refused while a run is recording,
since it drops the link and reopens it.

Ports with no hardware behind them are hidden by default. Linux creates
`/dev/ttyS0` through `ttyS31` whether or not the chips exist; the phantoms
report no UART in sysfs. On the development laptop that hides 32 of them and
leaves nothing, which is correct — it has no serial hardware at all.

The **Controller address** field lets a page served from one machine drive
another; the value is saved in the browser and the API sends permissive CORS
headers. The lab PC's IP has moved before.

A hosted static page cannot do this job — the UI has to reach a serial port,
so something with access to `/dev/tc600` must be in the loop.

`deploy/tc600-web.service` is a systemd unit for starting it at boot, and
`deploy/99-vacuum-lab.rules` is what makes `/dev/tc600` and `/dev/tpg252`
exist. Neither is installed by default, deliberately — see **Deploying to
the lab PC**.

### Safety

Every action that physically moves hardware is guarded three times: the page
asks for confirmation, the API refuses the request unless it carries an
explicit `confirm` flag, and the server-side interlocks refuse commands that
would sequence the rig in a damaging order.

| Interlock | Rule |
|---|---|
| Crossover | The turbopump may not start above 5×10⁻¹ mbar — measured, not timed, and refused outright if the pressure is unknown or stale. A *turning* rotor never authorises it: gas flow windmills the rotor when roughing starts |
| Stop dwell | The station may not stop until 20 s after the rotor reads 0 Hz |
| Vent speed | Venting is refused above 20 % of nominal speed |
| Cold cathode | A cold-cathode gauge will not be struck above 1e-2 mbar |
| Full speed | Speed setpoint and standby are locked out during a run |
| Inlet ceiling | A run aborts above its pressure ceiling — but only once the rotor is turning, since roughing legitimately starts at atmosphere |
| Phase timeouts | Every waiting phase gives up: roughing, the turbopump start, spin-up and the argon backfill after `stage_timeout_min`, the search for base pressure after `base_timeout_min`. The clock pauses while a guided run is waiting on a person |

A deposition also switches the plasma and the gas off on **every** exit path
— completion, abort, operator stop, internal error — and attempts each step
even if an earlier one failed, so "the magnetron would not switch off" cannot
also leave the argon running.

**Stopping a run asks what the pump should do.** Ending the recording and
powering the pump down are different intentions — one leaves the chamber
under vacuum for the next run, the other finishes for the day — so the panel
offers both rather than choosing silently. The power-down is a sequence, not
a command: the motor goes off, the rotor coasts to rest with the backing pump
still running, the stop dwell passes, and only then does the station stop.
It takes several minutes, reports the rotor speed as it goes, and if the
rotor will not come to rest it gives up with the backing pump **left
running**.

Interlocks can be overridden deliberately, because a safety rule with no
escape hatch is its own hazard once something has already gone wrong. Only
an **admin** may override: beside a blocked button, or after an interlock
refuses a command, an admin is offered **Override…**. It names the one rule
that stood in the way, and waives only that: if the command is then stopped by
a second rule, the admin is asked again and the second rule is added to the
first. It sends only after they type why it is safe now and tick that they
have checked the rig themselves. Every override is logged to stderr and into
the run's event log under their name, with the rules, their reason and the
tick. The test engine holds no override — a procedure that cannot start the
turbopump waits.

**Acknowledging a fault, or starting the pumping station, switches the
turbopump motor off first.** The drive restarts the rotor as soon as the
station and the motor are both on and no fault is latched, so clearing a fault
on a pump whose motor was left on is a turbopump start that nobody checked the
chamber for. If the motor cannot be switched off, nothing else is sent.
Acknowledging now asks for confirmation like every other action that moves the
pump. Starting the station when it is already on sends nothing.

**A reading that did not come back is never taken as good news.** If the
panel has not heard the rotor's speed, it does not treat the rotor as stopped:
the station stays on. A reading that is more than a few seconds old refuses
the same actions.

**A run's form refuses what makes no sense**: an empty box, a value that is not
a number, or one outside the field's range is refused with the field's name,
and Start stays off until it is fixed. A box left empty is never read as "no
limit". The panel checks the same things again before a run starts.

**A run is not started on a pump the panel cannot read or that has a fault
latched**, and while a run is going the pump can only be powered down through
**Stop** (which ends the run properly first). If the turbopump motor or the
station is switched off under a run — from the drive's front panel, say — the
run ends.

**Restarting or stopping the panel's service ends a running run with its
clean-up** (heater off, gas off), taking up to 20 s; the vacuum is left as it
is. If the lab PC loses power or the panel crashes mid-run, the run is marked
failed at the next start, its clean-up is run as soon as the pump answers, and
an alarm says so.

### Accounts

The lab network is shared, so the panel asks everybody to sign in, and what
you may do depends on your role in this chamber:

| Role | May |
|---|---|
| **viewer** | see everything — live state, history, runs, exports — and change nothing |
| **operator** | drive the rig: pump, gauge, runs, prompts. Of the drive's own settings, only the pump heater (on/off and its temperature, for a bake-out); every other drive setting is an admin's |
| **admin** | also set the rig up (equipment, ports, automatic recovery), approve accounts, reset passwords, pair tablets, change the drive's settings, **override interlocks** |
| **owner** | one per chamber: also appoints admins |

- **Getting an account.** *Request an account* on the sign-in page. Nothing
  happens until an admin approves it under Settings → People and picks your
  role. Ask for a higher one later from Settings → My account.
- **The first account** — the owner — is made on the lab PC itself, so nobody
  on the network can claim the rig: `./tools/users.py owner YOURNAME`. The
  same command gives a forgotten owner password back.
- **One person drives at a time.** The first operator to act takes
  **control**; the header says who has it. Control frees itself 15 s after
  that person's panel closes, they can **Release** it, and an admin can
  **Take over**. Without control you can still **make the pump safe**, **stop
  a run** (keeping the vacuum), cancel a power-down and acknowledge alarms.
- **Walking away.** After 15 minutes without a touch the panel only watches
  until you type your password again — 60 minutes while a run you started is
  going (`--idle-minutes`, `--run-idle-minutes`). On a lab tablet you are
  signed out instead, and the tablet goes back to watching. The tablet counts
  real taps and key presses only, and the timing is kept by the lab PC, so
  reloading the page does not give you more time. A sign-in on a tablet also
  ends after 8 hours at most, whatever you do.
- **Administering while idle.** An admin whose panel has gone idle can still
  open Settings → People, but the first thing they do there (approve an
  account, reset a password, pair a tablet) opens the password box; type your
  password and carry on.
- **After the lab PC restarts** (an update, a power cut), people who were
  signed in may have to type their password again. If the lab PC is not
  connected to network time, everybody does, and tablet sign-ins are dropped.
- **One login on two screens** is still one person in control: acting on the
  second screen never takes control from the first; **Take control here**
  does. Better still, give everyone who operates their own account — a
  shared one is fine only for watching.
- **Lab tablets** are paired once (Settings → People → Tablets gives a
  one-time code, typed into *Pair this tablet*). A paired tablet always shows
  the rig and sounds alarms, with nobody signed in, and can never act.
- Everything somebody does is recorded under their name in the event log.
- After an update, open panels — tablets and phones included — reload
  themselves within a poll or two.
- **Settings → Test the alarm sound** plays the alarm's two tones on the
  device in your hand, to check its speaker.
- **Messages you may meet.** *Busy, try again* means several people are
  signing in at the same moment; wait a few seconds and press again (it does
  not count as a wrong password). *Too many password checks* means that
  address, session or account has tried ten passwords in a minute; wait a
  minute. Wrong passwords repeated on one name from one address also lock
  that sign-in for a while.
- Reading the pump's diagnostics and accessory settings takes several
  seconds on a real drive. The page shows what was read in the last minute;
  **Read the drive** (or Refresh) reads again, for operators and above, at
  most every 30 seconds. Live pump values keep updating meanwhile.
- The connection is plain HTTP until the lab has a domain for a certificate:
  passwords cross the lab network unencrypted. Three options for whoever runs
  the server limit the exposure: `--allow-from NETWORKS` keeps every other
  network out entirely; `--admin-from NETWORKS` lets admin work (accounts,
  overrides, rig set-up, the drive's protective settings, taking over
  control) happen only from those networks, while everyone else on
  `--allow-from` can still watch and operate; and `--host-name NAME` /
  `--allow-origin URL` say which address names and which other web pages may
  talk to the panel (the panel answers "421" or "403" to anything else). The
  tablet on its cable (127.0.0.1) always passes. If Settings → Connection
  points at a panel on another machine, that machine must be started with
  `--allow-origin` for the page you are on.

---


## The deposition process

```
rough         forevacuum pump alone, atmosphere → crossover (5×10⁻¹ mbar)
crossover     at that pressure, and not before, start the turbopump
spinup        wait for full rotation speed
basepressure  keep pumping until the curve flattens — the chamber decides
backfill      throttle the gate valve, admit argon to the working pressure
presputter    strike the plasma with the shutter CLOSED, cleaning the target
deposit       open the shutter for the deposition time
recover       magnetron off, gas off, pump back down
```

Three things about this are deliberate:

**The turbopump runs flat out throughout.** Throttling is the gate valve's
job. A run writes `OpModeTMP = 0` at the start and locks the speed setpoint
and standby out for its duration — dropping the rotor to hold pressure would
cost compression exactly when the gas load is highest.

**Crossover is triggered by pressure, not a timer.** A turbopump started
against an un-roughed foreline cannot develop compression, draws its current
limit and trips on over-temperature. The gauge knows when the foreline is
ready; a stopwatch does not.

**Base pressure is measured, not assumed.** The run keeps pumping until the
pressure stops improving — a *fractional* rate, percent per minute, from a
fit of ln(p) against t. On a quantity spanning decades that is the only rate
worth quoting: 10⁻⁴ falling by 10⁻⁵/min and 10⁻⁶ falling by 10⁻⁷/min are the
same process at different depths.

### Guided and automatic

Nothing is wired to the argon, gate valve, magnetron or shutter yet, so each
starts in **manual** mode. Pick per run:

- **Guided** — the software drives what it can and stops at each manual step
  with a large, plain-language instruction and a *Done* button. It keeps
  sampling and keeps its abort checks live while it waits, so a chamber with
  the plasma on is never unmonitored.
- **Automatic** — refused up front unless every actuator is under software
  control, rather than failing at the first prompt with the chamber already
  pumped down.

Connecting an actuator later needs an object with `set()` and `read()` handed
to `ProcessHardware.attach()`. The recipes do not change.

## Runs and the database

Seven procedures, each recording a full curve into SQLite. The first two
are the rig's job; the rest are for commissioning and leak-hunting:

| Procedure | Reports |
|---|---|
| **Magnetron deposition (argon)** | The full process above; working-pressure stability, deposition time, whether it completed |
| **Base pressure calibration** | The chamber's real base pressure and time to reach it, and a warning if it is much worse than last time |
| **Full vacuum (hold until stopped)** | Pumps down and holds indefinitely, recording the whole time. Reports base and ultimate pressure, how long it held, peak current and temperature, and how far the pressure drifted while holding |
| **Pump-down curve** | Time to target, ultimate pressure, and the time at which each decade was crossed |
| **Rate of rise / leak test** | Least-squares dp/dt on an isolated chamber → leak rate in mbar·L/s, with R² |
| **Ultimate pressure / hold** | Lowest and final pressure, drift, and whether it has actually settled |
| **Bake-out / TMS profile** | Drives the pump's TMS through heat, soak and cool while logging pressure and temperature |

The rate-of-rise test reports R² and **refuses to call a bent curve a leak
rate**: below 0.95 the result carries a caveat saying the figure is an
outgassing transient, not a leak. That distinction is the point of the test.

**Full vacuum** is the one with no ending of its own: it pumps the chamber
out and stays there, for hours or days, until somebody stops it — and then
powers the pump down in the correct order. Its search for base pressure has
no deadline, but roughing and spin-up keep theirs, because a rig that cannot
reach vacuum is failing rather than working slowly. It watches the drive's
over-temperature flags on every sample, since a run that lasts days is a run
nobody is watching.

A run stores three things: the header and computed result, the sampled curve
(pressure on both channels and the best of them, rotor speed, current,
voltage, power, TMS temperature, fault code, station and motor state, the
drive's over-temperature flags, the argon flow, gate opening, magnetron power
and shutter state as far as they are known, and the base-pressure judgement's
own rate), and a narrative log of phase changes, interlock refusals and
operator actions. A manual step's value is recorded from the moment the
operator confirms it, not from when the software asked.

Two run rules come straight from the pump's limits. A flat curve counts as
base pressure only with the rotor at full speed — a plateau with the rotor
dragged down by gas describes the gas, not the chamber. And once the rotor has
reached full speed, **two minutes held below it by gas ends the run**, with the
gas shut off, rather than letting the drive trip on Err006 or over-temperature
minutes later.
Keeping the narrative separate means a run's story survives even when
sampling was patchy.

### Downloads

| Export | Contents |
|---|---|
| Per-run **CSV** | One run's curve, with a self-describing comment header |
| Per-run **JSON** | One run complete: header, config, result, events, curve |
| **All runs (CSV)** | One row per test with its result — the spreadsheet view |
| **Index (JSON)** | Every run's header and result, without the curves |
| **Full archive** | Every run *with* its curve |
| **SQLite file** | The exact database, taken as a consistent online backup |

Numbers are written at full precision, not display precision: a curve
re-analysed later must not be limited by how many digits the page showed.

The database lives at `~/.local/share/vacuum-lab/lab.db` unless `--db` says
otherwise. A run left open by a crash is closed out and marked failed on the
next start, so the UI never shows a phantom test that nothing is driving;
its clean-up (heater, gas) is run then, as soon as the pump answers.

---

## Bringing the gauge link up

**Current state (2026-09-01): both instruments are on FTDI USB adapters**,
reached as `/dev/tc600` and `/dev/tpg252`. The onboard UARTs stopped being
detected on 2026-08-31 — a BIOS or CMOS fault, not software — so the gauge
moved back onto the US232R cable that had been silent during the original
bring-up, and it works there.

The original bring-up is kept below, because the *method* is what makes a
silent serial link legible, and because the onboard ports may come back.

The TPG 252 A was originally on the PC's **onboard COM1 (`/dev/ttyS0`), not a
USB adapter**. Getting there took four attempts, and the dead ends are worth
recording because they are the ones most people hit:

| Attempt | Result |
|---|---|
| CH340 (`1a86:7523`) on `/dev/ttyUSB0` | silent — 0 bytes at every rate |
| Same, TX/RX swapped | silent — **rules out a crossover fault** |
| FTDI US232R (`0403:6001`), a genuine RS-232 cable | silent — **rules out a TTL-level adapter** |
| **Onboard COM1, `/dev/ttyS0`** | **answers at 9600 8N1** |

The FTDI attempt is the one that produced hard evidence. Its `ftdi_sio` driver
implements `TIOCGICOUNT`, which the CH340 driver does not, so the kernel's own
line counters could be read around a transmission:

```
delta:  tx: 5   rx: 0   frame: 0   parity: 0   overrun: 0   brk: 0
```

Five bytes went out; nothing came back; not one line error of any kind. That
is the signature of *nothing driving the receive line* — a live link at the
wrong baud rate produces framing errors, not silence. It is the same signature
that turned out to be swapped D+/D− on the pump side.

**If your gauge is silent, try the machine's real serial port before buying
another adapter.** An onboard 16550A has a proper RS-232 line driver, no USB
latency, and nothing to go wrong in between. `/proc/tty/driver/serial` lists
what the machine has; look for `uart:16550A` with a real I/O port.

```bash
./tpgctl.py -p /dev/tpg252 detect     # find the rate
./tpgctl.py -p /dev/tpg252 diagnose   # separate the failure modes
```

### What the driver supports

The TPG family speaks a **mnemonic protocol** with a two-leg handshake — the
command gets an acknowledgement, and the data only comes after a separate
enquiry byte:

```
host -> "PR1\r\n"        unit -> "<ACK>\r\n"
host -> "<ENQ>"          unit -> "0,1.2340E-05\r\n"
```

Implemented: pressures per channel and all at once, the seven measurement
status codes, gauge identification, gauge on/off, degas, measurement filter,
pressure unit (with automatic conversion to millibar for storage), the four
switching functions, the error register, the keylock, and baud-rate
autodetection.

No TPG 252 A manual is in this repository, so `tpg/commands.py` started as the
published *family* command set. The firmware settled it:

```bash
./tpgctl.py -p /dev/tpg252 probe
```

This sends only the bare query form of each mnemonic — it changes nothing —
and reports which the unit implements, which it refuses, and which it ignores.
The same button exists on the gauge's Equipment page.

Against firmware **BG509727-C** the answer was:

```
implemented (21): PNR TID BAU PRX PR1 PR2 UNI FIL CAL OFC OFD FSR SEN
                  PUC SPS SP1 SP2 ERR LOC TLC WDT
refused    (11): RSX NAD COR DGS SCX SP3 SP4 DCB DCC DCD DCS
```

Four places where the published family protocol is **wrong** for this unit,
each of which broke working code until the probe found it:

| Documented | Actual | Consequence |
|---|---|---|
| `SPx` → `channel,lower,upper` | `lower,upper` | `int("1.00E-11")` raised; the switching-function panel silently vanished |
| Four switching functions | **SP1 and SP2 only** | SP3/SP4 refused |
| `SEN` → 2 for a gauge that is on | **3** | a running gauge displayed as off |
| Degas available | `DGS` **refused** | the UI offered a control the unit rejects |

Also: an empty channel reports `noSe` (not `noSEn`), the fitted gauge
identifies as `CO9` — not in the published list, so the driver shows the raw
identifier rather than guessing — and `BAU` answers 4 while running at 9600,
so this model does not use the family's 0/1/2 baud encoding.

`tools/fake_tpg.py` models this unit including all four quirks, so
`tools/selftest.py` covers them.

---

## Command line tools

```bash
./tc600ctl.py ports          # which serial adapters does Linux see?
./tc600ctl.py scan           # find the pump's RS-485 address
./tc600ctl.py info           # identity + status
./tc600ctl.py monitor        # live display
./tc600ctl.py verify         # check the parameter table against the firmware
./tc600ctl.py -y on          # commands that move the pump need --yes

./tpgctl.py detect           # which baud rate does the gauge answer on?
./tpgctl.py info             # identity, fitted gauges, pressures
./tpgctl.py probe            # which mnemonics this firmware implements
./tpgctl.py monitor          # live pressure display
./tpgctl.py diagnose         # work out why a silent link is silent
./tpgctl.py log run.csv      # append pressure readings to CSV
```

Read and write individual parameters by name or number:

```bash
./tc600ctl.py read ActualSpd
./tc600ctl.py write GasMode 1
./tpgctl.py raw PRX
```

---

## Accessory connectors

The panel drives the drive unit's accessory connectors. What is reachable over
RS-485 was established by reading **every parameter number 0–999** on this unit
(firmware 017500); it implements 64.

| Connector | Control | Parameters |
|---|---|---|
| **HEAT/TMS** | full | P001 on/off, P704 setpoint 30–90 °C, P331 actual temp, P333 regulator engaged, P334 max temp, P335 type, P032 output use |
| **VENT** | full | P012 enable, P030 mode (controlled / none / on), P720 speed %, P721 open time |
| **FV PUMP** | partial | P025 mode (continuous / intermittent / delayed), P711 switch-on W, P710 switch-off W |
| **FAN** | none | — |

Three caveats worth knowing:

**The fan cannot be controlled over RS-485.** The air cooling unit plugs into
the drive unit's dedicated `FAN` connector and is driven by the TC 600 itself.
The full 0–999 sweep found no fan parameter, and the accessory-configuration
parameters other Pfeiffer units use for air cooling (P35–P38) are not
implemented on this firmware.

**The backing pump has no independent on/off.** It is switched through the relay
box by the pumping station (P010), so start it with *Start pumping station*.
P025/P710/P711 govern how it behaves once running.

**This unit is a TMS installation, not a conventional casing heater** — P335
reads 1. So the HEAT/TMS block is a temperature-regulated system with a
setpoint, not a plain on/off heater.

Accessory values are served on demand rather than in the status poll, because
they change rarely and reading them costs another 14 round trips. Writes go
through an allowlist (`ALLOWED_WRITES` in `lab/pump.py`) deliberately narrower
than "everything writable" — the RS-485 address, parameter set selection and
service line are not exposed behind a web button. Of what is on the page, an
**operator** may switch the HEAT/TMS heater and set its temperature (for a
bake-out); every other setting (venting, the forevacuum pump's mode, the
run-up monitoring and the rest) is an **admin's**, and the page shows an
operator only the heater. Every change is recorded with the old and the new
value.

---

## Verified working configuration

Confirmed talking to the real pump on 2026-07-30:

| | |
|---|---|
| Host | `user@labpc` (HP Compaq Elite 8300 CMT), key-based SSH |
| Port | `/dev/tc600` — FTDI FT232R, serial `BG03FP5F` (was onboard COM2, `/dev/ttyS1`, until 2026-08-31) |
| Chain | USB → HXSP-485 → TC 600 RS-485 socket |
| Address | 1 |
| Unit | TC 600, firmware 017500, nominal speed 1500 Hz |

The rear-panel COM1 (`/dev/ttyS0`, 0x3F8) did **not** work; `/dev/ttyS4` is Intel
AMT serial-over-LAN, not a physical port. There is no PCIe serial card in the
machine despite appearances — the second connector is a bracket off the
motherboard's COM2 header.

```bash
ssh user@labpc 'cd ~/tc600-control && ./tc600ctl.py -p /dev/tc600 info'
```

Getting there took two physical fixes: the converter had to move to COM2, and
**D+/D− were swapped**. Symptom of the swap was total silence — `rx:0` with zero
framing errors in `/proc/tty/driver/serial`, which is the signature of nothing
driving the receive line at all. Bytes arriving *with* framing errors would have
meant a live but misconfigured link instead.

## Deploying to the lab PC

The panel runs there as a systemd service, `tc600-web`, on
`http://labpc:8600/` (`labpc` standing for its address). Two files in `deploy/` set it up, and the udev
rules come first — the unit names devices that do not exist without them:

```bash
sudo cp deploy/99-vacuum-lab.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty --action=add
ls -l /dev/tc600 /dev/tpg252          # both should appear

sudo cp deploy/tc600-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tc600-web
```

**Names, not numbers.** The rules match each FTDI adapter's burned-in serial
number, so the name follows the cable rather than the USB socket:

| Name | Adapter | Goes to |
|---|---|---|
| `/dev/tc600` | FTDI FT232R, serial `BG03FP5F` | HXSP-485 → TC 600, RS-485 address 1 |
| `/dev/tpg252` | FTDI US232R, serial `FTYI6Y6H` | TPG 252 A, direct RS-232, 9600 8N1 |

`/dev/ttyUSB1` and `/dev/ttyUSB2` are handed out in enumeration order and
swap on a replug, which points the pump's RS-485 telegrams at the gauge; the
`/dev/serial/by-id/...` paths are stable but nobody types
`usb-FTDI_FT232R_USB_UART_BG03FP5F-if00-port0` correctly in a hurry, and a
wrong character in a `--serial` flag looks exactly like a dead instrument.
If an adapter is replaced, read its serial out of `udevadm info -a -n
/dev/ttyUSB0 | grep ATTRS{serial}` and edit the rules file.

**There is no `git` on the lab PC**, so updating it is done from the laptop
with `tools/release.py`, which checks, copies, switches over and, if the new
version does not come up healthy, switches back (see *Releasing a new
version* below). Each version lives in its own folder under
`~/vacuum-lab/releases/`, and the service runs `~/vacuum-lab/current`. The
old `~/tc600-control` folder is left in place as a fallback.

**The first update to this layout** is special: the old version writes no
status file, so the tool cannot tell whether the rig is idle — look yourself
first (no run, no power-down), then:

```bash
./tools/release.py deploy --to user@labpc --install-units --force-unknown-state
```

`--install-units` copies in the new service files, which point at
`~/vacuum-lab/current`. Prepare the backup stick first (see *Backups*), or the
panel shows "No backup of this database has been made" from its first start.
Every restart of the panel raises a "panel was restarted" warning to
acknowledge.

**The first update with accounts** needs the owner made before the restart,
or the panel comes back with nobody able to sign in (the pump carries on; only
the panel is locked):
`ssh -t user@labpc 'cd ~/vacuum-lab/current && ./tools/users.py owner YOURNAME'`.
Then pair each lab tablet (Settings → People → Tablets).

**The service file is locked down** (it may write only to the data
directory, and its files are readable by the service's user alone). Before
installing the new one check that `ls -ld ~/.local/share/vacuum-lab` shows
the directory — if it does not exist the service fails to start (status
226/NAMESPACE) and nothing watches the rig; `./tools/users.py owner NAME`
or `install -d -m 700 ~/.local/share/vacuum-lab` creates it — and that
`systemctl cat tc600-web` does not point `--db` somewhere else. Run
`timedatectl` too: when it does not say the clock is synchronised, every
update signs everybody out.

Two things to check before restarting. **Is the rig idle?** `/api/state`
reports the rotor speed and whether a run is recording; stopping the panel
does not stop the pump, but it does stop the interlocks and the run engine
watching it. **Is the unit file still true?** Ports can be rebound live from
the settings page and that only lives in memory, so a panel talking happily
to `/dev/tc600` can be a service whose unit still says `/dev/ttyS1` — the
restart is where that comes apart. Compare `systemctl cat tc600-web` with the
`port` field in `/api/state`.

The startup banner — ports opened, interlock limits, and whether an owner
exists yet — goes to `journalctl -u tc600-web`. It only gets
there because the unit sets `PYTHONUNBUFFERED=1`; without it Python block-
buffers stdout into a journal socket and the banner never appears.

## Releasing a new version

Run these from the laptop, in the project folder.

```bash
./tools/release.py check                          # the checks only, nothing is sent
./tools/release.py deploy --to USER@HOST          # check, send, switch over
./tools/release.py status --to USER@HOST          # which version runs, which are kept
./tools/release.py rollback --to USER@HOST        # back to the version before
```

`deploy` stops at the first thing that fails, and in this order it:

1. checks the files here: they must read as Python 3.11 to 3.14, and the self
   test and the browser test must pass (a tree with uncommitted changes is
   allowed, and the release is marked "dirty");
2. **refuses if the rig is busy** — a run going or a power-down under way,
   read from the panel's own status file. Wait, or stop the run first;
3. copies the version to the lab PC and runs the self test there, on its own
   Python;
4. makes a backup first;
5. compares the installed service files and device rules with the new ones
   and stops if they differ (`--install-units` installs them);
6. switches `current` to the new version and restarts the panel. Within a
   minute it must be running the new version, on the same ports, with no new
   critical alarm — otherwise it **switches back by itself** and says why;
7. keeps the five newest versions and removes older ones.

Only this folder travels: the tablet, the manuals and anything else at the
top of the repository are never sent. `rollback` does the same switch by hand, and also needs the rig idle.
The restart is announced on the panel as a warning to acknowledge.

## Backups

The SQLite download in the panel is **not** a backup: it leaves the accounts
out. `tools/backup.py` keeps everything — runs, samples, events, accounts and
the rig's settings — so a backup is as private as the database itself (files
readable by their owner only). Backups go to a USB stick on the panel's
machine and, when you want, to another machine; never only to the machine
that runs the panel, which may be off or broken.

**Setting up the stick** (once). Format it ext4 (a FAT stick cannot keep
files private), label it `VACUUMBAK`, and add this line to `/etc/fstab`:

```
LABEL=VACUUMBAK /mnt/vacuum-backup ext4 nofail,noatime,x-systemd.automount 0 2
```

Then, with the stick mounted, mark it as a backup folder and switch on the
timer, which makes a backup every six hours (and at boot, if one was missed):

```bash
./tools/backup.py init /mnt/vacuum-backup/vacuum-lab
sudo cp deploy/tc600-backup.service deploy/tc600-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tc600-backup.timer
```

The marking matters: a folder that was not marked by `init` is refused. If
the stick is not plugged in, the backup fails and the panel warns you, instead
of quietly filling the machine's own disk under the empty mount point.

**Looking at it.**

```bash
./tools/backup.py status                              # how the last backup went
./tools/backup.py verify /mnt/vacuum-backup/vacuum-lab  # check every backup on the stick
systemctl list-timers tc600-backup                    # when the next one runs
journalctl -u tc600-backup                            # how the last ones went
```

The panel warns when there is no backup, when the last one failed, or when the
last good one is more than 48 hours old. A copy to another machine is one more
`--dest HOST:FOLDER` on the timer's command (it goes by `rsync` over SSH with
your key); that machine tidies its own copies. From the laptop,
`./tools/backup.py pull HOST:FOLDER LOCAL_FOLDER` fetches a copy and checks it.

**How long they are kept.** Everything from the last 48 hours, then one a day
for 14 days, one a week for 8 weeks and one a month for 12 months. Nothing is
removed if a check fails, or if the machine's clock reads earlier than the
newest backup.

**Restoring.** Stop the panel first, then:

```bash
./tools/backup.py restore /mnt/vacuum-backup/vacuum-lab/chamber-1/lab-YYYYMMDD-HHMMSS --to ~/.local/share/vacuum-lab/lab.db
```

It refuses to overwrite an existing database unless you add `--force`.

**Every three months, practise.** A backup nobody has restored is a guess:

```bash
./tools/backup.py drill --folder /mnt/vacuum-backup/vacuum-lab
```

restores the newest good backup into a scratch folder, opens it and counts
what is in it, without touching the real database.

## The morning report

```bash
./tools/report.py                  # the last 24 hours
./tools/report.py --since 7d       # the last week
./tools/report.py --since 08:00    # since a time today (or a date, 2026-10-12)
```

It reads the database beside the running panel and prints, in plain text, the
runs that started or ended, the alarms (worst first), interlock overrides,
what people did, clean-ups still owed and how the backups are. It **exits with
status 1 when something needs a person** — a critical alarm, an owed clean-up,
a run cut off, or no good backup in 48 hours — so it can be put in a morning
`cron` job that mails you only when it matters.

---

## One-time setup: serial port permissions

On the lab PC the serial nodes are owned by group `dialout`; the account is
already in it. On another system they may be owned by `uucp`:

```bash
sudo usermod -aG uucp $USER    # then log out and back in
```

`id -nG` should then list the group. To test immediately without logging out:

```bash
sudo chmod a+rw /dev/ttyUSB0     # temporary, lost on replug
```

## Wiring

The TC 600's RS-485 port is an 8-pin modular (RJ45-style) socket. From
PM 0504 BE section 3.11:

| TC 600 pin | Signal |
|---|---|
| 2 | +24 V output (max 210 mA) |
| 5 | RS-485 D+ |
| 6 | GND |
| 7 | RS-485 D− |

The HXSP-485's RS-485 side is a DB9 male:

| HXSP-485 DB9-M pin | Signal |
|---|---|
| 1 | Data− |
| 2 | Data+ |
| 5 | GND |
| 6 | +5 to +12 V power input (optional) |

So: TC pin 5 → HXSP pin 2, TC pin 7 → HXSP pin 1, TC pin 6 → HXSP pin 5.

Both manuals ask for a 120 Ω termination resistor across Data+/Data− at each
end of the bus.

Line settings are fixed by the pump: **9600 baud, 8 data bits, no parity,
1 stop bit**. Factory default RS-485 address is **1** (parameter 797); 960 is
the group broadcast address.

## If nothing answers

Work down this list — the first two items are by far the most common.

1. **D+ / D− swapped.** Swap the two data wires and retry `scan`. This is
   harmless and fixes most first-time RS-485 setups.

2. **The HXSP-485 is not powered.** It has no power supply of its own; it
   steals power from the RS-232 handshake lines (TXD, DTR, RTS), and its manual
   requires those to swing above **+5 V**. This tool asserts DTR and RTS on
   open for exactly that reason.

   A **CH340** (`1a86:7523`) sold as "USB to RS232" is often actually **TTL
   level** (0/3.3 V or 0/5 V) with no RS-232 line driver, which is *not* enough
   to power the converter and is the wrong signalling level besides. Check with
   a multimeter, adapter plugged in, measuring against DB9 pin 5 (GND):

   - Pin 3 (TXD) idle should sit at roughly **−5 to −12 V** on true RS-232.
     If it reads 0 V or +3.3 V, the adapter is TTL and will not work here.
   - Pin 7 (RTS) and pin 4 (DTR) should read **≥ +5 V** while this tool has
     the port open.

   If the adapter is TTL, either get a true RS-232 adapter (FTDI FT232 with a
   MAX3232 stage, or a Prolific-based one), feed the HXSP-485 externally via
   DB9 pin 6 (+5 to +12 V), or skip the HXSP-485 entirely and use a direct
   USB-to-RS-485 dongle.

3. **Wrong address.** `./tc600ctl.py scan --first 1 --last 32`.

4. **Remote plug overrides the serial link.** PM 0504 BE notes that *all
   switched-on remote functions have priority over the serial interface*, and
   that pins 1–4 of the REMOTE connector ship bridged. If the pump reads fine
   but ignores `on`/`off`, that bridge is holding it. Parameter 60
   (`CtrlViaInt`) selects the control source.

5. **No termination**, or a bus longer than a metre or two with unshielded
   wire. Add the 120 Ω resistors.

## Choosing an adapter

The simplest fix for the whole chain is to drop the HXSP-485 and use a **direct
USB-to-RS-485 dongle**. That removes the parasitic-power problem entirely, and
the pump's own RS-485 port is already optically decoupled, so an isolated
(more expensive) dongle buys little here.

Chips whose Linux drivers are in the mainline kernel, so nothing to install:

| Chip | Driver | Notes |
|---|---|---|
| FTDI FT232R | `ftdi_sio` | Most reliable; hardware auto-direction. Watch for counterfeits. |
| Silicon Labs CP2102 | `cp210x` | Solid, cheap, widely stocked. |
| WCH CH340/CH341 | `ch341` | Cheapest. Fine at 9600 baud. |

Avoid **PL2303** clones: the mainline `pl2303` driver refuses counterfeit
HXA/TA chips outright, and most cheap ones are counterfeit.

If you buy a direct USB-to-RS-485 dongle, check whether it uses **RTS for
transmit-direction control** rather than automatic direction switching. On
those, holding RTS asserted keeps the transmitter enabled and the reply is
never heard, so run with:

```bash
./tc600ctl.py --no-handshake scan
```

Wiring is then A/B straight to the pump: dongle A → TC pin 5 (D+), dongle
B → TC pin 7 (D−), GND → TC pin 6. Vendors label A/B inconsistently, so if
`scan` finds nothing, swap the two.

## Verifying the parameter table

The manuals in `../docs/manuals/` cover wiring and the electrical spec, but defer
the parameter list to **PM 0488 BN** and **PM 0547 BN**, neither of which is in
this repository. The table in `tc600/params.py` is the standard Pfeiffer
parameter set, which the TC 600 implements — but it has not been checked
against your specific firmware.

Let the pump settle it:

```bash
./tc600ctl.py verify
```

This reads every parameter in the table and reports which ones the firmware
actually implements. Anything it does not know answers `NO_DEF` and is listed
as "not implemented". Trust that output over the table.

## Testing without hardware

```bash
./tools/selftest.py          # about 590 checks (about 11 min): drivers, analysis, database, accounts, history, alarms, run engine, bus faults, timing, restarts, who may do what, sign-in limits, port finding, logging, backups and releases
./tools/uitest.py            # the panel in a real browser (about 75 s, needs Chrome)
./tools/fake_rig.py          # both simulators, wired together
./tools/fake_tc600.py        # just the pump
./tools/fake_tpg.py          # just the gauge
./tools/selftest.py --only faults    # one section; --only can be repeated
./tools/selftest.py --coverage       # also lists the lines of the code no check reached
```

The self test runs one section at a time and puts everything back between
them (the simulators, the clocks, any thread left running), so one failing
section cannot spoil the next; it ends with a table by section and the list
of failed checks.

`tools/uitest.py` opens the panel in headless Chrome as each of the four roles
(viewer, operator, admin, paired tablet) at tablet and phone size, visits every
tab, and checks for script errors, failed requests, sideways scrolling, "NaN"
or "undefined" on screen, touch targets of at least 40 px and one poll every 2
seconds. It then walks through the main flows: signing in, what each role may
do, the override dialog, a guided prompt, a drive fault through to its
acknowledgement, the idle password prompt, a reload after an update. Add
`--shots DIR` to keep a screenshot of each page, and `--attach PORT` to
audit the page open on the lab's tablet.

The simulators can also misbehave on purpose — drop, corrupt or delay replies,
go silent, echo like some RS-485 converters, show the rig's own gauge layout —
so the tests can check what the panel does with a link that is not perfect.

`fake_rig.py` is the one that matters for anything touching runs: it feeds the
simulated rotor speed into the simulated chamber, so starting the pumping
station really does make the pressure fall and stopping it really does make it
rise again. Without that link each simulator is honest on its own and the pair
is nonsense — a chamber that pumps down while the pump is off proves nothing.

The chamber model is a real one:

```
dp/dt = (Q_leak + Q_outgas(t)) / V  −  S_effective · p / V
```

with a Pirani on channel 1 and a cold cathode on channel 2, each reporting
underrange, overrange and sensor-off at the right pressures. A simulated
pump-down runs 1013 → 7e-6 mbar in about 100 seconds.

The pump simulator behaves electrically like the real TMU 071: run-up at its
power limit (~82 W, ~3.8 A), ~12 W at speed with nothing to pump, gas friction
that grows with the gas the chamber hands it, the gas-mode-0 power line, and a
supply that sags under load. Too much argon drags its rotor down, raises
over-temperature and trips it with Err006 — a deposition in automatic mode
with 120 sccm and the gate open reproduces the failures of 2026-10-08.

## Layout

| Path | Purpose |
|---|---|
| `tc600/protocol.py` | TC 600 telegram framing, checksums, the eight data types |
| `tc600/params.py` | Parameter table and name/number lookup |
| `tc600/device.py` | RS-485 transport, retries, echo handling |
| `tpg/protocol.py` | TPG mnemonic protocol, ACK/ENQ handshake, status codes |
| `tpg/commands.py` | Command table, enumerations, gauge types |
| `tpg/device.py` | RS-232 transport, capability probe, baud autodetection |
| `lab/registry.py` | Device framework — one class per instrument |
| `lab/pump.py` | TC 600 service and process interlocks |
| `lab/gauge.py` | TPG service and the cold-cathode interlock |
| `lab/runs.py` | The seven procedures and the run engine |
| `lab/db.py` | SQLite store for runs, samples and events |
| `lab/history.py` | The rolling 24 h chart recorder, and the decimation that keeps every extreme |
| `lab/alarms.py` | The supervisor: alarms, and the pump's safe state after a fault |
| `lab/discovery.py` | Finding each instrument's port by asking every port |
| `lab/trends.py` | Per-run health indicators measured from the stored curves |
| `lab/judgement.py` | The thresholds a pressure curve and a rotor are judged by, kept once for the engine, the trends and the panel |
| `lab/logs.py` | Errors in the panel's own threads, logged once each with their traceback |
| `lab/export.py` | CSV / JSON / SQLite downloads |
| `lab/api.py` | HTTP routing and static serving |
| `web/` | Preact SPA, no build step |
| `web/chart.js` | The plotting component behind every chart, with its table view |
| `web/analysis.js`, `web/rigcharts.js`, `web/runcharts.js` | What the charts compute, and the chart sets for the rig, a run, and runs over time |
| `webserver.py` | Entry point |
| `tc600ctl.py` / `tpgctl.py` | Command line tools |
| `tools/` | Simulators, the self test (`tools/checks/`) and the browser test |
| `tools/backup.py`, `tools/release.py`, `tools/report.py` | Backups, releasing a new version, the morning report |
| `deploy/` | The service files (`tc600-web`, `tc600-backup`) and the device rules |

## Protocol notes

### TC 600

Telegrams are plain ASCII terminated by CR:

```
AAA AC PPP LL DATA... CCC <CR>
 |  |   |   |   |      +--- checksum: sum of preceding ASCII bytes mod 256
 |  |   |   |   +---------- payload, LL characters
 |  |   |   +-------------- payload length, 2 digits
 |  |   +------------------ parameter number, 3 digits
 |  +---------------------- action: 00 = read, 10 = write/reply
 +------------------------- address, 3 digits
```

A read request sends `=?` as its payload. Errors come back as `NO_DEF`
(no such parameter), `_RANGE` (value out of range) or `_LOGIC` (access
violation).

One subtlety worth knowing if you extend this: a **write request and its reply
are byte-identical**, since both carry action code 10 and the pump echoes the
accepted value. Reads are distinguishable (request 00, reply 10). So on a
half-duplex converter that loops transmissions back, an echoed write cannot be
told from a reply by content alone. `tc600/device.py` learns whether the
converter echoes from the first read exchange and applies that to writes.

### TPG

A **two-leg handshake**, and getting it wrong is the classic failure:

```
host -> "PR1<CR><LF>"        the mnemonic, optionally with parameters
unit -> "<ACK><CR><LF>"      understood        (or <NAK> for refused)
host -> "<ENQ>"              "now send it"
unit -> "0,1.2340E-05<CR><LF>"
```

The unit answers the mnemonic with nothing but an acknowledgement; the data
only comes after a separate enquiry byte. Skipping the ENQ leaves the reply
sitting in the unit and every later exchange one frame out of step. Every
exchange in `tpg/device.py` therefore opens with `<ETX>`, which flushes the
unit's input buffer, so resynchronising costs one byte instead of a power
cycle.

Every pressure reading carries a status digit. Only status 0 means the number
after it is a measurement; 1–6 are underrange, overrange, sensor error, sensor
off, no sensor and identification error, and the accompanying value is
meaningless. This software carries that distinction all the way to the chart —
an invalid reading is never plotted or stored as a pressure.

## Why not LabVIEW or PV TurboViewer

Both were evaluated and rejected.

**LabVIEW.** `../vendor/ni-labview/ni-labview-2026-community-26.3.0_linux.zip` contains
only six 34–41 KB package-feed stubs for openSUSE, RHEL and Ubuntu — not
LabVIEW itself. They register NI's repositories, and the real install pulls
gigabytes from NI's servers. There is no CachyOS/Arch target.

Running it in Docker does not rescue this. The official images
([ni/labview-for-containers](https://github.com/ni/labview-for-containers)) are
headless CI/CD images: LabVIEWCLI operations only, explicitly "no display or
GUI required", and their licence *prohibits* using LabVIEW in a container "for
development purposes, including but not limited to creating, editing, or
modifying LabVIEW code". They are for mass-compiling and testing VIs in a
pipeline, not for building or running an instrument front panel. Hardware I/O
is not part of their documented scope.

A hand-rolled container would additionally have to solve NI-VISA, whose
`nikal`/`nipalk` kernel modules must be built against the **host** kernel — a
container shares the host kernel and cannot load its own. That means building
NI kernel modules for CachyOS regardless, which is the hard part of the job
with none of the benefit.

**PV TurboViewer.** A Windows .NET 2.0 application from 2005, bundled with a
.NET Framework redistributable of the same vintage. Wine is installed here, so
it is not impossible, but it means fighting COM-port mapping and a legacy
installer to get a read-only viewer.

Against any of that, both instruments speak plain ASCII at 9600 baud. Talking
to them directly is a few hundred lines, which is what this is.
