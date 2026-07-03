"""Diagnostics helpers for the Settings → Diagnostics card.

Gathers a self-service support snapshot (version, model, DB stats, feature flags, current
state) and reads the rotating log files both processes write under the data dir, so a user
hitting a problem can copy/download logs + context to attach to a GitHub issue — instead of
us asking them to dig through Docker / Home-Assistant add-on logs by hand.

Everything here is read-only and redacts obvious secrets + the VIN before it leaves the box.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db_reader

# Coordinate signal IDs stripped from the raw-signal dump before it leaves the box, so the
# bundle can be shared publicly without revealing where the car (home) is. 3724/3725 = lon/lat,
# 2190/2191 = fallbacks, 2/3 = the signed pair. Everything else in the dict is non-locating.
_GPS_SIGNAL_IDS = {"2", "3", "2190", "2191", "3724", "3725"}


def data_dir() -> Path:
    """The persistent data dir — parent of the DB (set via DB_PATH in run.sh, /data on the add-on)."""
    return Path(os.environ.get("DB_PATH", "/data/leapmotor_mate.db")).parent


POLLER_LOG = "mate-poller.log"
WEB_LOG = "mate-web.log"
_LOG_FILES = {"poller": POLLER_LOG, "web": WEB_LOG}


# ── redaction ────────────────────────────────────────────────────────────────
# Defensive: the app never logs credentials, but a diagnostics bundle is shared publicly,
# so scrub anything that smells like one, plus the 17-char VIN and e-mail addresses.
# Sensitive key names — bare or compound (private_key, access_token, refresh_token…), plus the
# Leapmotor device_id (a long account-bound identifier the API auth logs in the clear).
_SECRET_ROOT = (
    r'(?:passwords?|passwd|passphrase|pwd|pass|pins?|secrets?|tokens?|credentials?|auth|apikey|keys?|'
    r'device[_-]?ids?)')
# An optional word-prefix joined by `_`/`-` (access_token, private_key). The separator is REQUIRED,
# so words that merely CONTAIN a root (monkey, compass, passenger) are never matched.
_SECRET_KEY = r'(?:\w+[_-])?' + _SECRET_ROOT
# key=value / key: value, in plain OR JSON form (optional matching quotes around the key),
# value either a quoted string (spaces kept) or an unquoted run up to a delimiter.
_KV_SECRET_RE = re.compile(
    r'(?i)(["\']?)\b(' + _SECRET_KEY + r')\b\1?\s*[:=]\s*'
    r'''("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|[^\s,;}\n]+)''')
_AUTH_RE = re.compile(r'(?i)\bauthorization\b\s*[:=].*')   # whole header value to EOL
_BEARER_RE = re.compile(r'(?i)\bbearer\s+[\w.\-]+')         # "Bearer <token>" with no key=
# `*` in the local part also catches an already-partly-masked address (sil***@dxc.com → ***@***).
_EMAIL_RE = re.compile(r'\b[\w.+*-]+@[\w.-]+\.\w{2,}\b')
_VIN_RE = re.compile(r'\b([A-HJ-NPR-Z0-9]{17})\b')
# camelCase secret keys (no separator, so the compound regex above misses them): the Leapmotor
# remote-control field `operatePassword`, plus userToken/apiKey-style names. The CAPITALISED
# suffix is required, so plain words (compass, passenger, compassHeading) are never matched.
_CAMEL_SECRET_RE = re.compile(
    r'''(?:["']?)\b([a-z]\w*?(?:Password|Passwd|Pwd|Pin|Token|Secret|Credential|ApiKey|AuthKey))\b["']?\s*[:=]\s*'''
    r'''("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|[^\s,;}\n]+)''')
# A latitude/longitude PAIR in parentheses — the trip-start log "@ (45.4717, 1.5433)". Truncated to
# ~1 decimal (≈10 km) so a publicly-shared bundle can't pinpoint home. Only a paren-wrapped decimal
# pair matches, so SoC / kWh / efficiency numbers in the logs are left untouched.
_COORD_RE = re.compile(r'\(\s*(-?\d{1,3}\.\d)\d*\s*,\s*(-?\d{1,3}\.\d)\d*\s*\)')


def mask_vin(vin: str | None) -> str:
    if not vin:
        return "—"
    return f"{vin[:3]}…{vin[-4:]}" if len(vin) >= 8 else "…"


def _redact(text: str, vin: str | None = None) -> str:
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(2)}=***", text)
    text = _CAMEL_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)   # operatePassword=…
    text = _AUTH_RE.sub("authorization=***", text)
    text = _BEARER_RE.sub("bearer ***", text)
    text = _EMAIL_RE.sub("***@***", text)
    # The real VIN appears lowercase + glued inside the MQTT discovery topic
    # (leapmotor_mate_lfza…820), which the generic uppercase \b regex below can't see — so when we
    # know the car's VIN, replace it literally first, any case.
    if vin:
        text = re.sub(re.escape(vin), mask_vin(vin), text, flags=re.IGNORECASE)
    text = _VIN_RE.sub(lambda m: f"{m.group(1)[:3]}…{m.group(1)[-4:]}", text)
    text = _COORD_RE.sub(lambda m: f"({m.group(1)}…, {m.group(2)}…)", text)
    return text


# ── system snapshot ──────────────────────────────────────────────────────────
def build_system_info(version: str) -> dict:
    """Cheap (no live cloud call) support snapshot for the card + the bundle header."""
    vehicle, settings = db_reader.get_vehicle()
    db = db_reader._get()

    def _count(table: str) -> int:
        try:
            return db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        except Exception:  # noqa: BLE001
            return -1

    last = None
    try:
        last = db.execute(
            "SELECT recorded_at, soc, gear, charging FROM positions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001
        pass

    age_min = None
    if last and last["recorded_at"]:
        try:
            age_min = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(last["recorded_at"]))
                .total_seconds() / 60, 1)
        except (TypeError, ValueError):
            pass

    # Positions date-span — so a "my history vanished" report (e.g. vampire-drain empty, #63) can
    # be diagnosed at a glance: a span far shorter than expected + a non-zero retention = pruning.
    span = "—"
    try:
        r = db.execute("SELECT MIN(recorded_at) a, MAX(recorded_at) b FROM positions "
                       "WHERE recorded_at IS NOT NULL").fetchone()
        if r and r["a"] and r["b"]:
            days = round((datetime.fromisoformat(r["b"]) - datetime.fromisoformat(r["a"]))
                         .total_seconds() / 86400, 1)
            span = f"{r['a'][:10]} → {r['b'][:10]} ({days}d)"
    except Exception:  # noqa: BLE001
        pass

    return {
        "version": version,
        "model": (vehicle or {}).get("car_type") or "—",
        "year": (vehicle or {}).get("year") or "—",
        "vin_masked": mask_vin((vehicle or {}).get("vin")),
        "battery_kwh": settings.get("battery_capacity_kwh", "—"),
        "language": settings.get("language", "en"),
        "db_size_mb": round(db_reader.get_db_size_bytes() / 1048576, 1),
        "counts": {"trips": _count("trips"), "charges": _count("charges"),
                   "positions": _count("positions")},
        "poll_parked": settings.get("poll_parked", "30"),
        "poll_driving": settings.get("poll_driving", "10"),
        "retention_days": settings.get("positions_retention_days", "0"),
        "vampire_min_drop_pct": settings.get("vampire_min_drop_pct", "0.2"),
        "positions_span": span,
        "features": {
            "mqtt": settings.get("mqtt_enabled") == "1",
            "wallbox": bool(settings.get("ha_url") or os.environ.get("SUPERVISOR_TOKEN")),
            "abrp": settings.get("abrp_enabled") == "1",
            "addon": bool(os.environ.get("SUPERVISOR_TOKEN")),
        },
        "last_poll_iso": last["recorded_at"] if last else None,
        "last_poll_age_min": age_min,
        "last_soc": last["soc"] if last else None,
        "last_gear": last["gear"] if last else None,
        "last_charging": bool(last["charging"]) if last else None,
    }


# ── logs ─────────────────────────────────────────────────────────────────────
def read_log_tail(which: str, lines: int = 200) -> str:
    """Last `lines` of the poller/web log file (redacted). Returns a friendly note if absent."""
    name = _LOG_FILES.get(which)
    if not name:
        return f"(unknown log '{which}')"
    path = data_dir() / name
    if not path.exists():
        return ("(no log file yet — it appears after the next restart, once the file logger is "
                "active. Until then, see the container / Home Assistant add-on log.)")
    try:
        with path.open("r", errors="replace") as fh:
            tail = fh.readlines()[-max(1, min(lines, 2000)):]
        vehicle, _ = db_reader.get_vehicle()
        return _redact("".join(tail), (vehicle or {}).get("vin")).strip() or "(log is empty)"
    except Exception as e:  # noqa: BLE001
        return f"(could not read log: {e})"


# ── shareable bundle ─────────────────────────────────────────────────────────
_BUNDLE_PARTS = ("info", "poller", "web", "signals")   # user-selectable sections


def _signals_section(signals: dict | None, vin: str | None) -> str:
    """The car's raw signal dict as pretty JSON, with the GPS coordinate ids stripped and the
    usual secret/VIN redaction applied — so it's safe to drop straight into a shared bundle."""
    if not signals:
        return ("(no live signals — car asleep or unreachable; use the car briefly, then download "
                "again)")
    clean = {k: v for k, v in signals.items() if k not in _GPS_SIGNAL_IDS}
    return _redact(json.dumps(clean, indent=2, sort_keys=True), vin)


def _vampire_section() -> str:
    """What get_vampire_drain() actually computes — so an 'empty/missing battery-drain chart'
    report (e.g. #63) shows the real count/windows, not just the user's screenshot. Uses the SAME
    `min_drop_pct` the battery page does, so the bundle reproduces what the user sees: a high
    threshold that charts nothing shows count=0 here too, with measurable>0 revealing the cause."""
    try:
        mdp = float(db_reader.get_setting("vampire_min_drop_pct", "0.2") or 0.2)
        v = db_reader.get_vampire_drain(min_drop_pct=mdp)
    except Exception as e:  # noqa: BLE001
        return f"(vampire calc failed: {e})"
    out = [f"count={v.get('count')}  measurable={v.get('measurable_count')}  "
           f"below_threshold={v.get('below_threshold')}  min_drop_pct={v.get('min_drop_pct')} %/day  "
           f"typical={v.get('typical_pct_per_day')} %/day  lookback={v.get('lookback_days')}d"]
    for w in (v.get("windows") or [])[-15:]:
        out.append(f"  {str(w['start'])[:16]} → {str(w['end'])[:16]}  {w['drop_pct']}% / {w['hours']}h "
                   f"= {w['pct_per_day']} %/day  reliable={w['reliable']}"
                   + ("  ongoing" if w.get("ongoing") else ""))
    return "\n".join(out)


def _soc_daily_section() -> str:
    """Per-day SoC hi→lo + km driven for the last 14 days — reveals the parked-drain pattern and
    any data gaps behind a 'my history vanished' report (#63)."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        rows = db_reader._get().execute(
            "SELECT substr(recorded_at,1,10) d, COUNT(*) n, MIN(soc) lo, MAX(soc) hi, "
            "MIN(odometer_km) o0, MAX(odometer_km) o1 FROM positions "
            "WHERE soc IS NOT NULL AND recorded_at >= ? GROUP BY d ORDER BY d", (cutoff,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return f"(soc-daily failed: {e})"
    out = []
    for r in rows:
        drove = (r["o1"] or 0) - (r["o0"] or 0)
        out.append(f"  {r['d']}  n={str(r['n']):<5} soc {r['hi']:.1f}→{r['lo']:.1f}  drove {drove:.0f} km")
    return "\n".join(out) or "(no SoC samples in the last 14 days)"


def _cost_wallbox_section() -> str:
    """Pricing config (mode per type + base prices + dynamic sensors), wallbox entity mapping, and the
    last few charges (type · DC · AC/wallbox · cost) — so a 'why is my cost X, kWh Y' report (#109)
    can be diagnosed without guessing. No credentials: entity names + kWh/cost figures only."""
    def _num(x):
        try:
            return f"{float(x):.2f}"
        except (TypeError, ValueError):
            return "—"
    lines = []
    try:
        cfg = db_reader.get_cost_config()
        prices = db_reader.get_charge_prices()
        modes = cfg.get("modes") or {}
        lines.append(f"Cost method  : {cfg.get('method', '-')} · legacy mode {cfg.get('mode', '-')} "
                     f"· TOU bands {len(cfg.get('bands') or [])}")
        lines.append("Mode / type  : " + ", ".join(
            f"{t}={modes.get(t, cfg.get('mode', 'flat'))}" for t in ("HOME", "AC", "FAST", "HPC")))
        lines.append("Base €/kWh   : " + (", ".join(f"{k}={v}" for k, v in sorted(prices.items()))
                                          or "(none set)"))
        for t in ("HOME", "AC", "FAST", "HPC"):
            if modes.get(t, cfg.get("mode", "flat")) == "dynamic":
                lines.append(f"Dynamic sensor ({t}): "
                             f"{db_reader.get_dynamic_price_entity_for(t) or '(none)'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(cost config unavailable: {e})")
    try:
        we = db_reader.get_setting("wallbox_entities", "")
        lines.append("Wallbox map  : "
                     + (we or "(no entities mapped → no AC energy → HOME cost billed on DC/SoC)"))
        lines.append(f"Auto-HOME    : {db_reader.get_setting('wallbox_auto_home', '0')}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(wallbox config unavailable: {e})")
    try:
        db = db_reader._get()
        rows = db.execute(
            "SELECT started_at, location_type, energy_added_kwh, ac_energy_kwh, cost, "
            "wallbox_energy_start_kwh, reconstructed FROM charges WHERE ended_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 8").fetchall()
        lines.append("")
        lines.append("Last charges — DC (battery) · AC (wallbox) · cost · wb baseline · "
                     "show_wb=does-the-card-show-AC · raw ac_energy value (#109 diagnosis):")
        for r in (rows or []):
            ac_raw = r["ac_energy_kwh"]
            # EXACT card condition (charges.html `show_wb`): a truthy AC energy on a HOME charge.
            # If show_wb is False here but cost was billed on AC, ac_energy was lost between billing
            # and render — the smoking gun for "cost on AC, card shows DC".
            show_wb = bool(ac_raw) and r["location_type"] == "HOME"
            lines.append(
                f"  {(r['started_at'] or '')[:16]} {(r['location_type'] or '-'):5} "
                f"DC={_num(r['energy_added_kwh'])} AC={_num(ac_raw)} cost={_num(r['cost'])} "
                f"wb_start={_num(r['wallbox_energy_start_kwh'])} show_wb={show_wb} "
                f"raw_ac={ac_raw!r} recon={r['reconstructed']}")
        if not rows:
            lines.append("  (no charges)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(charges unavailable: {e})")
    return "\n".join(lines)


def build_bundle(version: str, parts=_BUNDLE_PARTS, lines: int = 300, signals: dict | None = None) -> str:
    """One redacted text blob to attach to an issue. `parts` selects which sections to include
    (any of 'info', 'poller', 'web', 'signals'); a one-line version header is always present. The
    'signals' section dumps the car's raw signals with GPS coordinates stripped (caller passes a
    freshly-fetched signal dict) so the whole bundle stays safe to share publicly."""
    want = {p for p in parts if p in _BUNDLE_PARTS} or set(_BUNDLE_PARTS)
    out = [f"===== LeapMotor Mate {version} — diagnostics ====="]

    if "info" in want:
        info = build_system_info(version)
        f = info["features"]
        out += [
            f"Model / year : {info['model']} / {info['year']}",
            f"VIN          : {info['vin_masked']}",
            f"Battery kWh  : {info['battery_kwh']}",
            f"Language     : {info['language']}",
            f"DB size (MB) : {info['db_size_mb']}",
            f"Rows         : trips={info['counts']['trips']} "
            f"charges={info['counts']['charges']} positions={info['counts']['positions']}",
            f"Poll (s)     : parked={info['poll_parked']} driving={info['poll_driving']}",
            f"Positions    : span {info['positions_span']} · retention {info['retention_days']}d (0=keep all)",
            f"Vampire thr  : min_drop {info['vampire_min_drop_pct']} %/day (chart display threshold)",
            f"Features     : mqtt={f['mqtt']} wallbox={f['wallbox']} abrp={f['abrp']} addon={f['addon']}",
            f"Last poll    : {info['last_poll_iso']} (age {info['last_poll_age_min']} min) "
            f"soc={info['last_soc']} gear={info['last_gear']} charging={info['last_charging']}",
        ]
        out += ["", "----- battery standby / vampire-drain (computed) -----", _vampire_section()]
        out += ["", "----- SoC by day (last 14d · hi→lo · km driven) -----", _soc_daily_section()]
        out += ["", "----- cost & wallbox config -----", _cost_wallbox_section()]
    if "poller" in want:
        out += ["", "----- poller log (recent) -----", read_log_tail("poller", lines)]
    if "web" in want:
        out += ["", "----- web log (recent) -----", read_log_tail("web", lines)]
    if "signals" in want:
        vehicle, _ = db_reader.get_vehicle()
        out += ["", "----- raw signals (GPS removed) -----",
                _signals_section(signals, (vehicle or {}).get("vin"))]
    out += ["", "===== end ====="]
    return "\n".join(out)
