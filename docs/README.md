# docs

Reference material for the rig and the panel. The code in
[`../control/`](../control/) cites these by section where a limit or a
protocol detail comes from them.

### Manuals

| File | What it is | Used for |
| --- | --- | --- |
| [`manuals/pm0504be_m.pdf`](manuals/pm0504be_m.pdf) | Pfeiffer **PM 0504 BE/M** — TurboDrag pump TMH/TMU 071 with the electronic drive unit **TC 600**, operating instructions (30 pages) | the pump's ratings (supply, start-up time, gas-mode power line), the RS-485 wiring, the accessory connectors |
| [`manuals/Pfeiffer_TPG262_Operating_Instructions.pdf`](manuals/Pfeiffer_TPG262_Operating_Instructions.pdf) | Pfeiffer **BG 805 196 BE** — DualGauge **TPG 262** operating instructions (106 pages); the TPG 252 A on the rig speaks the same protocol | the RS-232 mnemonics, measurement and error codes |
| [`manuals/HXSP-485_RS-232_RS-485_UserManual.pdf`](manuals/HXSP-485_RS-232_RS-485_UserManual.pdf) | the **HXSP-485** RS-232 ↔ RS-485 converter between the lab PC and the TC 600 | its pinout and D+/D− wiring |

The TC 600's full parameter list lives in Pfeiffer's **PM 0488 BN** and
**PM 0547 BN**, which are not here. The table in `control/tc600/params.py`
was checked against the firmware instead (`./tc600ctl.py verify`). The real
TPG 252 A firmware also differs from the published protocol in a few places;
`control/README.md` lists them.

### The panel

* [`control-panel-manual.html`](control-panel-manual.html) — the operators'
  manual of the web panel, one page, printable. Open it in a browser.

### Images

* [`images/`](images/) — screenshots of the panel in its simulator, used by
  the READMEs. To make new ones, run the simulator and pass `--shots DIR` to
  `control/tools/uitest.py`.
