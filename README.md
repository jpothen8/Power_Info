# Power Monitor

A lightweight macOS menu bar app showing live power flow.

The menu bar title is a single number: **net battery power flow** —
`+31.1W` while charging, `-26.7W` while discharging — or a plain `▪`
when plugged into a capable adapter at high charge (see
[Idle indicator](#idle-indicator) below). Still fully clickable either
way. Clicking it opens an animated flow graphic: input streams on the
left (AC adapter and/or battery), output streams on the right (system,
each USB-C device charging off the Mac, and/or the battery), with dots
traveling the streams that fade from yellow (input) to blue (output).
It keeps updating live even while the dropdown is open.

## Poll cadence

Two independent rates, both driven off a single 3 s timer tick:

- **SMC (the menu bar title + graphic wattage)**: every 3 s, always —
  cheap (no subprocess), so there's no reason to slow it down.
- **`ioreg` (battery %, voltage, adapter identity, USB-out wattage)**:
  every **10 s** normally, tightening to **3 s** (matching SMC)
  whenever a USB device is actively charging, so that number stays
  just as live when it matters. `ioreg` has no "watch" mode — each
  call is a ~11 ms subprocess spawn — so polling it at a fixed fast
  rate regardless of whether anything USB-related is happening would
  just be wasted spawns for no visible benefit.

## Why two data sources (SMC + ioreg)

The original design polled only `ioreg -rw0 -r -n AppleSmartBattery`.
In testing, that felt "stuck" — confirmed empirically in this project:
polling it every second for 20+ seconds straight, `SystemPowerIn`,
`SystemLoad`, and `BatteryPower` never changed once. That telemetry
block is a **cache** populated by the battery gauge IC on its own
schedule (observed: 20+ seconds between updates), not a live feed —
`ioreg` can't force it to sample faster.

Real-time wattage now comes from **direct SMC (System Management
Controller) reads** via IOKit (`smc.py`), the same approach used by
[WattSec](https://github.com/beutton/wattsec) and
[exelban/stats](https://github.com/exelban/stats) — confirmed live on
this machine (values change second-to-second under sub-second polling):

- `PSTR` ("System Total") → system draw
- `PDTR` ("DC In") → adapter input power
- Battery net flow is **derived** as `PDTR - PSTR` (adapter surplus
  charges the battery; a deficit is drawn from it) rather than trusted
  from the SMC battery-rail key directly, since that key's sign
  convention wasn't independently confirmed.

`ioreg` is still used for values that don't need to be sub-second and
have no SMC equivalent found on this hardware: battery %, voltage,
adapter identity, and per-port USB-out wattage (`PowerOutDetails`) —
checked the full SMC sensor key list; none of these exist as raw
electrical sensors, since USB-out is PD-negotiation state, not a
sensor rail. If SMC access ever fails (e.g. a future macOS
restriction), the app falls back to pure `ioreg` numbers automatically
rather than crashing.

### Why total in / out watts can disagree

The graphic's "In X W" / "Out X W" footer captions aren't always
equal, and the reason is specific rather than generic sensor noise:
`laptop_w` (the "System" stream) is computed as
`max(system_w - usb_out_w, 0)`. `system_w` (SMC's `PSTR`) is fresh
every 3 s; `usb_out_w` (ioreg's `PowerOutDetails`) can lag up to 10 s
behind — most likely right after a USB device is unplugged, before
the next `ioreg` poll catches up. If the stale `usb_out_w` is larger
than the current `system_w`, the `max(..., 0)` clamp can't subtract
all of it back out, and total output exceeds total input by exactly
`usb_out_w - system_w` until the next `ioreg` poll corrects it —
confirmed by construction: with `usb_out_w` fresh (no staleness), the
totals cancel out exactly and always match, verified against 2000
random inputs. Real conversion losses (charging isn't 100% efficient)
and independent-sensor sampling skew are also true in principle, but
this staleness window is the one actually reachable in this app.

## Idle indicator

The title shows a plain `▪` instead of a wattage number when
`is_idle_at_full_charge()` (in `parser.py`) holds: plugged into a
**decent charger** and battery **≥ 97%** — full stop. It deliberately
doesn't look at the instantaneous battery flow at all: near-100%
charge-controller trickle jitter is noise regardless of its exact
wattage, so there's nothing worth gating on there. "Decent charger"
means a known adapter rating of at least 60 W (`IDLE_MIN_ADAPTER_WATTS`)
— below that is phone/tablet-charger territory, not something that
comfortably powers a MacBook; an unrated/unrecognized adapter is
treated as *not* decent (fails closed — shows the number rather than
assuming). Both thresholds (`IDLE_SOC_THRESHOLD`,
`IDLE_MIN_ADAPTER_WATTS`) are constants at the top of `parser.py` — no
settings GUI, just edit and restart.

## Install & run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python power_monitor.py
```

The app runs as a plain Python process — no Xcode, no code signing.
Quit from the dropdown menu or Ctrl-C in the terminal.

### Start at login

```bash
./install_launch_agent.sh     # starts automatically at every login
./uninstall_launch_agent.sh   # removes it
```

This generates a `com.jpothen.powermonitor.plist` (with your repo's
absolute path baked in) and installs it as a per-user LaunchAgent
(`~/Library/LaunchAgents`). `RunAtLoad` is on;
`KeepAlive` is off, so quitting it from the menu doesn't respawn it —
it'll come back at your next login. Logs go to `logs/`.

It runs as `.venv/bin/power_monitor` rather than `.venv/bin/python` —
a symlink to the same interpreter, created purely so it shows up as
**`power_monitor`** in Activity Monitor / `ps` instead of a generic
"Python" indistinguishable from any other Python process running on
the machine. Recreate it if the venv is ever rebuilt:
`ln -sf python .venv/bin/power_monitor`.

To restart the running instance by hand (e.g. after an update):

```bash
launchctl kickstart -k gui/$(id -u)/com.jpothen.powermonitor
```

(`launchctl bootstrap` run from a non-interactive/automation context
may not fire `RunAtLoad` immediately the way a real login does — the
`kickstart -k` command above always works regardless; the registration
itself is unaffected either way.)

## Resource usage

Measured on this machine (M4 Max):

- **Idle CPU**: 0.0–0.1%, ~75 MB steady RSS, no growth observed across
  many poll cycles
- **SMC tick** (every 3 s, always): ~0.3 ms — no subprocess
- **`ioreg` tick**: ~11 ms subprocess spawn, at 10 s intervals
  normally (amortized ~0.1% duty cycle) or 3 s while a USB device is
  actively charging (~0.37%, matching the SMC rate)
- **Per animation frame** (only while the dropdown is open, 30 fps):
  ~0.6 ms to render, i.e. under 2% of one core even in the worst case

In short: negligible either way. The dominant cost is the `ioreg`
subprocess spawn, and it now only pays that cost as often as the data
it fetches actually needs to be fresh.

## Tests

```bash
.venv/bin/python test_parser.py   # parsing, SMC overlay, stream layout, idle detection
.venv/bin/python test_smc.py      # SMC struct layout & byte decoding
# or: python3 -m pytest
```

`test_parser.py` fixtures encode ground-truth numbers from real
machine snapshots, including the discharging case where `ioreg`
reports `BatteryPower` as a 64-bit unsigned wraparound (e.g.
`18446744073709524916` = -26.7 W), plus a full unmodified live capture
in `fixtures/live_sample.txt`. `test_smc.py` pins down the one part of
the SMC path that can go silently wrong: the 80-byte struct layout
(an extra padding field some ports add breaks every call with
`kIOReturnBadArgument`) and the per-type byte decode (`flt` is
little-endian; `ui8/16/32` and the `spXX` fixed-point types are
big-endian) — confirmed against live hardware reads in this session.

## Files

- `power_monitor.py` — rumps app: menu bar title, animated flow
  graphic, adaptive-cadence timer (runs on `NSRunLoopCommonModes` so it
  — and the animation — keep updating while the dropdown menu is open)
- `parser.py` — pure parsing/calculation functions (unit-testable, no
  deps): `ioreg` parsing, the SMC overlay, the flow-graphic stream
  layout, and the idle/full-charge detector
- `smc.py` — direct SMC key reads via IOKit/ctypes (no external
  binary or Swift helper needed)
- `test_parser.py`, `test_smc.py` — tests
- `fixtures/live_sample.txt` — real `ioreg` capture used as a fixture
- `install_launch_agent.sh`, `uninstall_launch_agent.sh` —
  launch-at-login support; the LaunchAgent plist is generated on
  install (not checked in, since it embeds an absolute path)

## Notes

- `ioreg` output is Apple's custom debug format (nested `{}`/`()`/`<>`),
  not plist/JSON; the parser extracts keys with scoped balanced-brace
  scanning, never crashes on missing fields, and shows `⚠️` in the menu
  bar if reads fail (it keeps retrying).
- "System draw" is the laptop itself, excluding USB-C device charging:
  telemetry shows `SystemLoad`/`PSTR` already includes USB-out power,
  so the dropdown/graphic subtract it back out.
- v1 non-goals: no settings GUI, no disk logging, MacBook-only
  (`AppleSmartBattery`/Apple Silicon SMC keys as probed on this
  machine — Intel Macs may expose different key names).
