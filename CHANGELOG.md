# Changelog

All notable changes to LeapMotor Mate are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.0] — 2026-06-04

### Added
- **Charge type confirmation.** A new charge is no longer silently assumed to be
  "Home": until you set its type it shows a "To confirm" badge (with a "What type
  of charge?" prompt), and the Charges page shows how many are still pending. A
  charge enters the wallbox comparison only once you confirm it as Home.

### Changed
- **The wallbox comparison is now scoped to Home charges**, so it stays correct
  with multiple EVs sharing one wallbox and with public/away charging. History,
  totals and the per‑charge overlay only consider Home charges (a wallbox charges
  one car at a time, so a Home session means this car was on the wallbox);
  public/away and unconfirmed charges are excluded.
- The wallbox **live panel** now shows session metrics only while the car is
  plugged in — otherwise the live reading could be another vehicle on the same
  wallbox. Session cost and max available power are always shown.

## [1.1.1] — 2026-06-04

### Fixed
- **Wallbox in add‑on mode** — the add‑on now correctly detects the Home Assistant
  Supervisor token. On the s6‑overlay base images the Supervisor‑provided
  environment (including `SUPERVISOR_TOKEN`) isn't passed to the service process,
  so the add‑on fell back to the standalone URL+token form and showed "not
  connected". `run.sh` now loads it from the s6 container environment, and logs
  whether the HA API is available at startup.

## [1.1.0] — 2026-06-04

### Added
- **Wallbox integration (Home Assistant)** — optional. Pair a wallbox already in
  Home Assistant to get a dedicated **Wallbox** page with: a live panel (power,
  status, session energy, charging speed, max available power) plus the session
  cost; a control to set the wallbox max charging current; and an **AC‑vs‑DC
  comparison** per charge session — kWh delivered by the wallbox vs kWh into the
  battery, with charging efficiency — as a year/month/day history with an
  expandable power chart. Connects automatically via the Supervisor API when run
  as an add‑on (any external access mode — HTTP, HTTPS, Nabu Casa), or via an HA
  URL + a Long‑Lived Access Token when standalone (self‑signed HTTPS is fine).
  Enable and configure it in **Settings → Wallbox present** (live connection
  status + an entity picker limited to your wallbox's own sensors).

### Changed
- **Trips page redesign** — trip rows show a remaining/used SOC bar, a coloured
  efficiency pill and a route thumbnail; the dashboard gained four summary tiles
  (total distance, trips, average efficiency, regen). Trip distance now comes from
  the odometer delta (more accurate than GPS).
- **Vehicle page** restyled to match the rest of the app (slate cards/tiles).
- **Settings** reorganised into three columns; the Wallbox card stays minimal when
  disabled and reveals the HA connection + an expandable sensor list when enabled.
- Quantities across the UI are shown to at most two decimals, at full precision
  (no over‑rounding).

## [1.0.8] — 2026-06-02

### Added
- **Charging-power chart** in the Charges page. Each session has an expandable
  "Charging power" section that lazy-loads an inline chart of power over time
  (with SOC on a second axis). Power is the same value as the official
  charging-power reading (battery voltage × current) and is kept at full
  precision so the real curve is visible — most useful on DC fast charging.

### Changed
- **Settings layout**: cards now use a masonry column layout, removing the empty
  gap that appeared under shorter cards (e.g. Language) next to taller ones.

## [1.0.7] — 2026-06-02

### Added
- **Language selector in Settings**. The language could previously only be chosen
  in the initial setup wizard, so already-installed users had no way to switch.
  Settings now has a language dropdown (🇬🇧 English / 🇮🇹 Italiano / 🇫🇷 Français);
  changing it saves immediately and reloads the page in the new language.

## [1.0.6] — 2026-06-02

### Added
- **French language** (🇫🇷). The setup wizard now offers three languages — English,
  Italian and French — with three flag buttons and auto-detection of French
  browsers. The whole app is translated: overview, trips, charges, commands,
  statistics, the vehicle page, and both wizard steps (certificate + account login).

### Fixed
- Two certificate-step labels (`app.crt`, `app.key`) were hard-coded in English
  regardless of the chosen language; they are now translated (this also fixes
  Italian, where they were previously shown in English too).

## [1.0.5] — 2026-06-02

### Fixed
- **Poller self-recovery**: if the account TLS certificate temp file vanished from
  `/tmp` (every poll then failed with "Could not find the TLS certificate file"),
  the poller stayed stuck in an error loop indefinitely. It now forces a fresh
  login to re-create the certificate on cert/auth/token/connection errors (rate-
  limited to ~once per minute). Also recovers from auth/token drops.

## [1.0.4] — 2026-06-02

### Fixed
- **Local time in the UI**: trip/charge times were shown in UTC; they are now
  converted to the local timezone (`TZ`, which Home Assistant passes to add-ons
  automatically; standalone Docker sets `TZ` in compose). Added `tzdata`.
- **Trip fragmentation**: a drive was split into many records because a trip ended
  after just ~20s of zero speed. Trip detection is now gear-based and matches the
  HA reference: a trip ends only when gear **P** is held ~1 min (red lights / brief
  stops in gear D no longer split it), and movements **< 0.5 km** are discarded.

## [1.0.3] — 2026-06-02

### Fixed
- **Statistics**: the "Consumption trend (6 weeks)" chart legend showed week
  dates as `MM-DD` (US-style); they are now formatted as `DD/MM`.

## [1.0.2] — 2026-06-02

### Added
- **Vehicle page**: new sidebar page with live tyre pressure (per corner, with
  low-pressure alarms), door and window open/closed states, panoramic roof and
  battery/cabin temperatures — styled as gradient status cards.

### Fixed
- `find_car` was calling a non-existent client method; now driven through the
  registered remote action so it reaches the car.
- Install docs spell out the exact add-on repository URL.

## [1.0.1] — 2026-06-01

### Fixed
- **Home Assistant ingress support**: the web UI now works inside the add-on
  panel. URLs are resolved against the ingress path via `<base href>` (from the
  `X-Ingress-Path` header) and all template/JS URLs are relative; server
  redirects carry the ingress prefix. Standalone is unaffected.

## [1.0.0] — 2026-06-01

First public release.

### Added
- Trip tracking with route map, distance, energy, efficiency and regen.
- Charge logging with AC/DC detection, energy added, power and distribution chart.
- Statistics: driving/AC/other energy split and a 6-week consumption trend (Leapmotor cloud).
- Remote control: lock, windows, trunk, panoramic roof, climate, find car, battery preheat.
- Two-step setup wizard: app certificate (upload/paste) + account login with EU model/battery auto-detect.
- Configurable polling (parked/driving), bilingual UI (EN/IT).
- Home Assistant add-on and standalone Docker deployment.

[1.0.2]: https://github.com/ProtossBlaster/leapmotor-mate/releases/tag/v1.0.2
[1.0.1]: https://github.com/ProtossBlaster/leapmotor-mate/releases/tag/v1.0.1
[1.0.0]: https://github.com/ProtossBlaster/leapmotor-mate/releases/tag/v1.0.0
