# Changelog

All notable changes to LeapMotor Mate are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

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
