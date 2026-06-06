# Model-aware capability profile (B10)

Mate shows only what **this** car actually supports. A per-VIN capability profile classifies
each feature, and the UI / MQTT hide what is confirmed broken — **never** hiding the CORE
telemetry that trips, charges, reports and charts depend on.

## The two-axis model

A **SENSOR** (the car reports the state) and a **COMMAND** (we can actuate it remotely) are
**independent**. On the B10 the comfort family is the archetype: the state sensor works
(reflects manual activation) but the remote command is *accepted-but-not-executed*. So they
are separate features — e.g. `seat_heat` (sensor, shown read-only) vs `seat_heat_cmd`
(command/button, hidden when broken).

Verdicts: `working` (proven), `broken` (confirmed accepted-but-not-executed, or a dead sensor),
`untested` (unknown → shown; we never hide on a guess). Stored per VIN in the `settings`
table as `capabilities_<vin>`.

## What the API does / doesn't do on the B10 (empirical)

Tested live on the car: commands via the public `leapmotor-api` `_remote_control_raw`, effects
read back from `get_fresh_signals`. Contributed upstream: **markoceri/leapmotor-api#3**.

### CORE — always work, never hidden
soc, range, odometer, vehicle state, gear, speed, GPS, charge state/power, plug, inside &
battery temperature, tyres, doors, trunk, lock, sunshade, window open/closed.

### Climate
- Quick **COOL / HEAT / VENTILATION** (cmd_id 170, `operate=manual`): **command works**.
- Full A/C **OFF**: **broken** — neither the `ac_switch` toggle nor cmd_170 `operate=close`
  ever drives `1938 acSwitch → 0`. Worse: on the B10 `operate=close` flips the HVAC into
  **AUTO** and leaves it running, so it can be *worse than a no-op*. A true full-off is only
  possible from the car's physical control. (Best remote mitigation: `operate=manual` +
  `windlevel=1` — fan at minimum, avoids the AUTO ramp. Still doesn't power off.)

### Comfort — SENSOR works / COMMAND broken
Seat heating (driver `2100` / passenger `2118`), seat ventilation (`2101` / `2119`),
steering-wheel heat (`1816`), mirror heat (left `49` / right `50`). The state sensors reflect
manual activation instantly; the remote commands return success (`code=0`) but never move the
signal — verified both **parked** and **in READY**. → Mate shows these as **read-only
sensors** (the Comfort side card) and hides the control buttons.

### Others
- **sentry** (`3636`): command broken.
- **window opening-%** (`3727/3728/1879/1880`): sensor **dead** on the B10 (windows physically
  open but %=0) → hidden. Window open/closed *state* works.
- **cmd_id recon:** `420` is accepted but inert in every state; `340` = native charge-limit that
  actuates (`{"chargesoc":80}`, audible relay); `410` ON3 is vehicle-gated (signal `1258`
  bcmKeyPositionOn3 is driven only by the physical key); `361` = read-only prepare-car schedule.

### Not exposed on the B10 at all
Outside/ambient temperature, tyre temperature, window opening-%.

## How it's wired

- `web/capability_profile.py` (+ a copy in `poller/`, per the `session_share.py`/`crypto.py`
  duplication convention): the feature registry + `is_shown()` logic, with a parameterized
  settings accessor so both the web app (`db_reader`) and the poller (`db.get_setting`) can use it.
- The **poller** writes the live comfort states each poll as `comfort_state_<vin>` in `settings`
  (no `positions` schema change), so the web UI — which reads the positions row, not raw signals
  — can display them.
- **MQTT discovery** hides the broken *A/C Off* button on the B10 (clears its retained config so
  Home Assistant drops it) and publishes the working comfort sensors; everything gated per-VIN by
  `is_shown`.
- The **Commands page** shows a read-only **Comfort** side card with the comfort sensors as tiles
  (same tile style as the rest of the page; real car MDI icons).

## Status

### Done
- Capability registry (two-axis) + persisted B10 verdicts.
- MQTT: A/C-Off hidden on the B10; comfort sensors published (gated).
- Commands page: Comfort side card (page-style tiles, MDI car icons), placed beside the controls
  block at the same height.
- Battery card display fix (show `NN°`; stop the header texts overlapping when narrow).
- (Separate work this session) HA install-path docs fix for the 2026.2 *Apps* rename, and an
  add-on-repo auto-sync CI workflow.

### To do
- Publish to GitHub when authorized (currently committed locally on `prebuild`).
- More at-the-car probes to generalise: seat heat `301` `{"value":"1,3"}` (sensor `2100`) and
  seat vent `370` (sensor `2101`) in READY; re-test sentry `220` / defrost `460` in READY.
- Wire `is_shown` into more of the UI/MQTT surface as new commands get exposed.

## Notes
Detailed reverse-engineering of the official app (static decompile / dynamic unpacking — both
blocked by the 360 Jiagu packer and its anti-emulator self-kill) is kept in **local notes**
outside this public repository, for legal reasons. This document covers only the empirical,
behavioural findings, which are already public via upstream issue #3.
