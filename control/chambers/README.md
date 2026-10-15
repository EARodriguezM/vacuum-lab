# chambers

One file per vacuum chamber: which instruments it has, on which ports, with
which settings. The panel reads one at start —
`./webserver.py --chamber chambers/<file>.toml`, `chamber-1.toml` when none
is named — and checks it strictly: a mistake stops the panel with the file
and the field named, instead of starting it on a default nobody chose.

The files are [TOML](https://toml.io/), read with Python's own `tomllib`;
comments (`#`) are welcome — say why a setting is what it is.
[`chamber-1.toml`](chamber-1.toml) is the example to copy:

```toml
id = "chamber-1"
name = "Chamber 1"
about = "Magnetron sputtering in argon"

[instruments.pump]
kind = "tc600"
port = "auto"
address = 1
settle_ms = 30

[instruments.gauge]
kind = "tpg"
port = "auto"
channels = 2
```

| Field | Meaning |
| --- | --- |
| `id` | Lower-case letters, digits and dashes. Accounts' roles are granted per chamber id, so keep it once people have accounts |
| `name` | What the panel calls the chamber |
| `about` | A line saying what the chamber is for (optional) |
| `[instruments.<role>]` | One table per instrument. A role left out is not there: the panel starts without it, and every procedure that needs it is switched off |

### Roles

| Role | Needed by |
| --- | --- |
| `pump` — the turbopump's drive unit | every procedure except *Ultimate pressure / hold*, which drives nothing |
| `gauge` — the gauge controller | every procedure: the crossover interlock and every curve need the pressure |

### Kinds

Each instrument has a `kind` — which driver talks to it — and a `port`: a
device path, or `"auto"` to find it by asking every serial port with
read-only queries.

| Kind | Role | Options (default) |
| --- | --- | --- |
| `tc600` | `pump` | `address` — RS-485 address, 1–255 (1); `settle_ms` — pause before listening for a reply, for the RS-485 converter to turn round, 0–1000 (30) |
| `tpg` | `gauge` | `baudrate` — leave it out to detect it; `channels` — measurement channels, 1–6 (2) |

A chamber with an instrument there is no driver for yet leaves that role out
until there is one — and gives the other instruments' ports, or starts with
`--no-port-search`: a port search asks every free port, the unknown
controller's included. The survey on the Settings page asks only for the
kinds the file lists. A new driver goes in `../drivers/`, its service in
`../lab/`, and its kind in `KINDS` in `../lab/chamber.py`.

### Overriding for one start

`--serial`, `--address` and `--pump-settle-ms` (the pump), `--gauge`,
`--gauge-baud` and `--gauge-channels` (the gauge), `--no-pump`, `--no-gauge`,
`--chamber-id` and `--chamber-name` take precedence over the file.
