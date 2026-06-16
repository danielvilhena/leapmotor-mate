# Model-aware capability profile (B10)

Mate shows only what **this** car actually supports. A per-VIN capability profile classifies
each feature, and the UI / MQTT hide what is confirmed broken — **never** hiding the CORE
telemetry that trips, charges, reports and charts depend on.

## The two-axis model

A **SENSOR** (the car reports the state) and a **COMMAND** (we can actuate it remotely) are
**independent** — they're classified separately (e.g. `seat_heat` the sensor vs `seat_heat_cmd`
the command). A sensor can work while its command doesn't, or vice-versa, so each gets its own
verdict and the UI/MQTT decide per-axis what to expose.

Verdicts: `working` (proven), `broken` (confirmed accepted-but-not-executed, or a dead sensor),
`untested` (unknown → shown; we never hide on a guess). Stored per VIN in the `settings`
table as `capabilities_<vin>`.

## What the API does / doesn't do on the B10 (empirical)

Tested live on the car: commands via the public `leapmotor-api` client, effects read back from
the fresh signals. The hard cases (A/C full-off, comfort) were cracked on-car + with payloads
captured by **@kerniger** (leapmotor-ha #41/#42); we shared the B10 READY PID `1258` back, and
the A/C-off finding went upstream as **markoceri/leapmotor-api#3** (closed).

### CORE — always work, never hidden
soc, range, odometer, vehicle state, gear, speed, GPS, charge state/power, plug, inside &
battery temperature, tyres, doors, trunk, lock, sunshade, window open/closed, **READY** (signal
`1258` bcmKeyPositionOn3, driven only by the physical key).

### Climate — WORKING on the B10
- Quick **COOL / HEAT / VENTILATION** (cmd_id 170, `operate=manual`): **work**.
- Full A/C **OFF**: **SOLVED** (shipped v1.11.3). The B10 powers the A/C fully off with
  **`ac_switch` + `{"operate":"off"}`**, which drives `1938 acSwitch → 0` (confirmed on-car,
  ≤3 s). NB the library's old `ac_off()` sent `operate=close`, which on the B10 only flips HVAC
  to **AUTO** (never powers off) — that was the source of the old "can't turn it off" reports.
- Target **temperature** stepper 18–32 °C (cmd 170, auto cool/heat vs cabin temp): works.

### Comfort — SENSOR **and** COMMAND working (since v1.11.4)
Both axes work on the B10 using the payloads captured by @kerniger (leapmotor-ha 0.6.11):
- **Seat heating** (301) / **seat ventilation** (370): `{"position":"driver|copilot","level":"0..3"}`
  — actuates sensors driver `2100`/`2118` (heat) and `2101`/`2119` (vent); levels 0–3 map exactly.
- **Steering-wheel heat** (320): ON `{"level":"2"}` / OFF `{"level":"1"}` — sensor `1816`.
- **Mirror heat** (440): ON `{"value":"2"}` / OFF `{"value":"1"}` — sensors `49`/`50`.

→ Mate exposes these as full **controls** (per-seat 0–3 level sliders; steering & mirror on/off
toggles) on the Commands page, plus the read-only state. The international payloads are shared
across C10/B10 (no B10-specific flow); our earlier failure was sending `position` as a numeric
index instead of the string `"driver"/"copilot"`.

### Windows — WORKING on the B10, on a 0–10 scale (cmd 230)
The window open/close command (`cmd 230`, `{"value":N}`) actuates the B10 — but on a **0–10 scale**,
not the 0–100 the `leapmotor-api` docstring implies. Mapped on-car (windows in a closed box):
- **only `0 / 2 / 5 / 10` move the car** = closed / ~20% (vent) / ~50% / fully open;
- every other value (1, 3, 4, 6, 7, 8, 9, and 25/50/100…) is **accepted by the cloud
  (`"请求成功"`) but ignored by the car** — so it's effectively 4 discrete stops, not continuous.
- The T03 is the documented 0–100 continuous scale (per markoceri/leapconnect).

Mate maps a uniform 0–100% UI to each model's native value (B10 ÷10, T03 ×1) and snaps the B10
slider to the 4 valid stops. Because the B10's **position sensor is dead** (above), the Vehicle
page shows the last *commanded* position as the %, gated by the real open/closed flag.

### Still broken / unsupported on the B10
- **sentry** (`3636`, cmd 220): command accepted (`code=0`) but never actuates.
- **window opening-% sensor** (`3727/3728/1879/1880`): **dead** on the B10 — reads 0 even with the
  windows physically open (verified on-car at 50%) → not shown. The open/closed **flag** (`1693–1696`:
  0 = closed, 2 = open) *does* work. NB the open/close **command itself works** on the B10 (see the
  Windows note in §"What works") — only the *position read-back* is missing, so Mate shows the last
  *commanded* position as the %.
- **`unlock_charger`** (charge-port unlock, right 192): exposed in v1.11.5 (web + MQTT) but its
  on-car actuation on the B10 is **not yet confirmed** — may turn out accepted-but-not-executed
  like the old A/C-off; pull/gate it if so.
- Minor: defrost engages heating but signal `1945` doesn't move.
- **cmd_id recon:** `340` = native charge-limit that actuates (`{"chargesoc":80}`); `410` ON3 is
  vehicle-gated (only the physical key raises `1258`); `420` accepted-but-inert; `361` = read-only
  prepare-car schedule.

### Not exposed on the B10 at all
Outside/ambient temperature, tyre temperature, window opening-%.

## How it's wired

- `web/capability_profile.py` (+ a copy in `poller/`, per the `session_share.py`/`crypto.py`
  duplication convention): the feature registry + `is_shown()` / `command_shown()` logic, with a
  parameterized settings accessor so both the web app (`db_reader`) and the poller
  (`db.get_setting`) can use it.
- The **poller** writes the live comfort states each poll as `comfort_state_<vin>` in `settings`,
  so the web UI can display them and drive the comfort tiles.
- **MQTT discovery** publishes the working command buttons (climate incl. A/C Off, comfort, find
  car, unlock charge cable) and the comfort sensors, each gated per-VIN by `command_shown` — a
  button confirmed `broken` on a car has its retained config cleared so Home Assistant drops it.
- The **Commands page** shows the comfort controls (sliders/toggles) and a battery/quick-actions
  card, all in the unified MDI-icon tile style (post-v1.11.5 restyle).

## Status (current: v1.11.5)

### Done
- Capability registry (two-axis) + persisted B10 verdicts.
- **A/C full-off** solved (`operate=off`, v1.11.3) and **all B10 comfort commands working**
  (seat heat/vent levels, steering & mirror heating, v1.11.4) — none hidden anymore.
- **READY** indicator (signal `1258`) on the Overview battery card.
- MQTT: comfort + climate (incl. A/C Off) + find car + unlock charge cable buttons published
  (gated); comfort sensors published.
- Commands page restyle (v1.11.5): MDI icons from one source, uniform tiles, balanced two-column
  layout, per-card headers.

### To do
- Confirm **`unlock_charger`** actually actuates on the B10 (on-car); gate/pull if it's a no-op.
- **sentry (220)** and **window open/close & window-%** still accepted-but-not-executed — could
  ask @kerniger for those payloads too (he supplied the comfort ones).
- Wire `is_shown`/`command_shown` into more of the UI/MQTT surface as new commands get exposed.

## Notes
Detailed reverse-engineering of the official app (static decompile / dynamic unpacking — both
blocked by the 360 Jiagu packer and its anti-emulator self-kill) is kept in **local notes**
outside this public repository, for legal reasons. This document covers only the empirical,
behavioural findings, which are already public via upstream issue #3.
