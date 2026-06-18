"""Read-only DB queries for the web layer."""
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import os

import i18n
import crypto  # hard import at module top: a missing crypto dep must fail web boot loudly,
              # never silently degrade a per-request secret read
import capability_profile

# Timestamps are stored in UTC (poller uses datetime.now(timezone.utc)); the UI
# must show local time. Standalone Docker sets TZ in compose → use it. As an HA
# add-on TZ is usually NOT in the env (the Supervisor only sets the container's
# local time via /etc/localtime), so fall back to None → astimezone(None) honours
# the system local time = your HA timezone. No hardcoded Europe/Rome (that made
# every non-Italian user see the wrong time).
try:
    from zoneinfo import ZoneInfo
    _tz = os.environ.get("TZ")
    _LOCAL_TZ = ZoneInfo(_tz) if _tz else None
except Exception:
    _LOCAL_TZ = None


def _local_dt(s) -> Optional[datetime]:
    """Parse a stored UTC timestamp and return it as an aware datetime in the
    local timezone. Returns None if the value is missing/unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace(" ", "T").rstrip("Z"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ)


def _local_iso(s):
    """Convert a stored UTC timestamp string to a local-time ISO string, so that
    template slices like started_at[11:16] display local time. Falls back to input."""
    dt = _local_dt(s)
    return dt.isoformat() if dt else s

# In-memory optimistic overlay: after a command, keep the expected state for
# _OPT_TTL seconds so the poller can't overwrite it before the UI refreshes.
_opt_overrides: dict = {}
_opt_expiry: float = 0.0
_OPT_TTL = 30


# Labels are intentionally language-neutral (international loanwords + universal
# electrical acronyms) so they never need translating across UI languages.
CHARGE_TYPES = {
    "HOME": {"label": "Home", "icon": "🏠", "color": "#22c55e"},
    "AC":   {"label": "AC",   "icon": "🔌", "color": "#60a5fa"},
    "FAST": {"label": "DC",   "icon": "⚡", "color": "#fb923c"},
    "HPC":  {"label": "HPC",  "icon": "🚀", "color": "#e879f9"},
    "FREE": {"label": "FREE", "icon": "🆓", "color": "#a3e635"},
    "MANUAL": {"label": "Manual", "icon": "✎", "color": "#94a3b8"},
}

PRICE_KEYS = {
    "HOME": "price_home_kwh",
    "AC":   "price_ac_kwh",
    "FAST": "price_fast_kwh",
    "HPC":  "price_hpc_kwh",
}

def auto_location_type(max_power_kw: float) -> str:
    p = max_power_kw or 0
    if p <= 8:   return "HOME"
    if p <= 22:  return "AC"
    if p <= 80:  return "FAST"
    return "HPC"


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


DB_PATH = os.environ.get("DB_PATH", "leapmotor_mate.db")


def _get():
    return _conn(DB_PATH)


def _conn_rw() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(key: str, default: str = "") -> str:
    db = _get()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db = _conn_rw()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    db.commit()


def get_db_size_bytes() -> int:
    """Total on-disk size of the SQLite DB (main file + WAL/SHM sidecars)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    return total


def get_trip_track(trip_id: int) -> list[dict]:
    """Full ordered GPS track for one trip (for GPX export — not downsampled). Group-aware: a
    merged trip returns the union of all its segments' tracks, in chronological order."""
    db = _get()
    ids = _segment_ids(db, trip_id)
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        "SELECT recorded_at, latitude, longitude, speed_kmh, soc FROM trip_positions "
        f"WHERE trip_id IN ({ph}) AND latitude IS NOT NULL AND longitude IS NOT NULL "
        "ORDER BY recorded_at, id",
        ids,
    ).fetchall()
    return [dict(r) for r in rows]


def checkpoint() -> None:
    """Flush the WAL into the main DB file so a file copy/download is consistent."""
    c = _conn_rw()
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.commit()
    finally:
        c.close()


def get_secret(key: str, default: str = "") -> str:
    """Read a secret setting, decrypting transparently (plaintext passes through)."""
    return crypto.decrypt(get_setting(key, default))


def set_secret(key: str, value: str) -> None:
    """Write a secret setting encrypted at rest (matches the poller's crypto/key)."""
    set_setting(key, crypto.encrypt(value or ""))


def get_or_create_device_id() -> str:
    """One stable device_id for this Mate install, shared by poller and web.
    Must match the poller's value so the whole app is a single Leapmotor device on
    the shared app cert (a random per-login device_id kept evicting other clients).
    INSERT OR IGNORE so poller and web converge on the same value."""
    import uuid
    did = get_setting("mate_device_id")
    if not did:
        db = _conn_rw()
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
            ("mate_device_id", uuid.uuid4().hex),
        )
        db.commit()
        did = get_setting("mate_device_id")
    return did


def is_setup_complete() -> bool:
    return get_setting("setup_complete") == "1"


def get_language() -> str:
    return get_setting("language", "en")


# ── Currency ──────────────────────────────────────────────────────────────────
# Monetary amounts are formatted via the Jinja `money` filter using this table.
# Stored setting `currency` holds the ISO 4217 code; default EUR keeps the old
# behaviour. `pos` = symbol placement, `dec` = decimal digits. Names stay in
# English (international convention) so they need no translation.
CURRENCIES = {
    "EUR": {"name": "Euro",            "symbol": "€",   "pos": "after",  "dec": 2},
    "USD": {"name": "US Dollar",       "symbol": "$",   "pos": "before", "dec": 2},
    "GBP": {"name": "British Pound",   "symbol": "£",   "pos": "before", "dec": 2},
    "CHF": {"name": "Swiss Franc",     "symbol": "CHF", "pos": "before", "dec": 2},
    "SEK": {"name": "Swedish Krona",   "symbol": "kr",  "pos": "after",  "dec": 2},
    "NOK": {"name": "Norwegian Krone", "symbol": "kr",  "pos": "after",  "dec": 2},
    "DKK": {"name": "Danish Krone",    "symbol": "kr",  "pos": "after",  "dec": 2},
    "PLN": {"name": "Polish Złoty",    "symbol": "zł",  "pos": "after",  "dec": 2},
    "CZK": {"name": "Czech Koruna",    "symbol": "Kč",  "pos": "after",  "dec": 2},
    "HUF": {"name": "Hungarian Forint","symbol": "Ft",  "pos": "after",  "dec": 0},
    "RON": {"name": "Romanian Leu",    "symbol": "lei", "pos": "after",  "dec": 2},
    "BGN": {"name": "Bulgarian Lev",   "symbol": "лв",  "pos": "after",  "dec": 2},
    "HRK": {"name": "Croatian Kuna",   "symbol": "kn",  "pos": "after",  "dec": 2},
    "TRY": {"name": "Turkish Lira",    "symbol": "₺",   "pos": "before", "dec": 2},
    "CAD": {"name": "Canadian Dollar", "symbol": "$",   "pos": "before", "dec": 2},
    "AUD": {"name": "Australian Dollar","symbol": "$",  "pos": "before", "dec": 2},
    "NZD": {"name": "New Zealand Dollar","symbol": "$", "pos": "before", "dec": 2},
    "JPY": {"name": "Japanese Yen",    "symbol": "¥",   "pos": "before", "dec": 0},
    "CNY": {"name": "Chinese Yuan",    "symbol": "¥",   "pos": "before", "dec": 2},
    "INR": {"name": "Indian Rupee",    "symbol": "₹",   "pos": "before", "dec": 2},
    "BRL": {"name": "Brazilian Real",  "symbol": "R$",  "pos": "before", "dec": 2},
    "MXN": {"name": "Mexican Peso",    "symbol": "$",   "pos": "before", "dec": 2},
    "ZAR": {"name": "South African Rand","symbol": "R", "pos": "before", "dec": 2},
    "RUB": {"name": "Russian Ruble",   "symbol": "₽",   "pos": "after",  "dec": 2},
    "UAH": {"name": "Ukrainian Hryvnia","symbol": "₴",  "pos": "after",  "dec": 2},
    "ILS": {"name": "Israeli Shekel",  "symbol": "₪",   "pos": "before", "dec": 2},
    "KRW": {"name": "South Korean Won","symbol": "₩",   "pos": "before", "dec": 0},
    "SGD": {"name": "Singapore Dollar","symbol": "$",   "pos": "before", "dec": 2},
    "HKD": {"name": "Hong Kong Dollar","symbol": "$",   "pos": "before", "dec": 2},
    "THB": {"name": "Thai Baht",       "symbol": "฿",   "pos": "before", "dec": 2},
    "MYR": {"name": "Malaysian Ringgit","symbol": "RM", "pos": "before", "dec": 2},
}
_DEFAULT_CURRENCY = "EUR"


def get_currency_code() -> str:
    code = get_setting("currency", _DEFAULT_CURRENCY)
    return code if code in CURRENCIES else _DEFAULT_CURRENCY


def get_currency() -> dict:
    """Full metadata dict for the configured currency (always valid)."""
    return CURRENCIES[get_currency_code()]


def set_currency(code: str) -> None:
    if code in CURRENCIES:
        set_setting("currency", code)


def get_charge_prices() -> dict:
    db = _get()
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'price_%_kwh'"
    ).fetchall()
    return {r["key"]: float(r["value"]) for r in rows}


# ── Charging-cost configuration (flat 24h vs time-of-use bands) ───────────────
# Stored in `settings`: cost_mode = 'flat'|'tou', tou_method = 'split'|'start',
# tou_bands = JSON list of {start, end, prices:{HOME,AC,FAST,HPC}}. The flat
# price_*_kwh values double as the "off-band" price in time-of-use mode.
_TOU_TYPES = ["HOME", "AC", "FAST", "HPC"]


def get_cost_config() -> dict:
    """Pricing config for the Costs page: mode, calc method and the user bands."""
    raw = get_setting("tou_bands", "")
    try:
        bands = json.loads(raw) if raw else []
        if not isinstance(bands, list):
            bands = []
    except (ValueError, TypeError):
        bands = []
    return {
        "mode":   get_setting("cost_mode", "flat"),
        "method": get_setting("tou_method", "split"),
        "bands":  bands,
    }


def save_cost_config(mode: str, method: str, bands: list) -> None:
    """Persist the Costs-page config. Bands are sanitised to {start,end,prices}."""
    mode   = mode   if mode   in ("flat", "tou")   else "flat"
    method = method if method in ("split", "start") else "split"
    clean = []
    for b in bands or []:
        if not isinstance(b, dict):
            continue
        start = str(b.get("start", "")).strip()
        end   = str(b.get("end", "")).strip()
        if not start or not end:
            continue
        prices, src = {}, (b.get("prices") or {})
        for t in _TOU_TYPES:
            try:
                prices[t] = round(float(src.get(t)), 4)
            except (TypeError, ValueError):
                prices[t] = None
        # Days of the week the band applies to (0=Mon … 6=Sun). Empty/invalid =
        # every day, so a band always applies somewhere.
        raw_days = b.get("days")
        days = sorted({int(d) for d in raw_days
                       if isinstance(d, (int, float)) and 0 <= int(d) <= 6}) \
            if isinstance(raw_days, list) else []
        if not days:
            days = list(range(7))
        clean.append({"start": start, "end": end, "days": days, "prices": prices})
    set_setting("cost_mode", mode)
    set_setting("tou_method", method)
    set_setting("tou_bands", json.dumps(clean))


def _parse_hhmm(s) -> Optional[int]:
    """'HH:MM' → minute-of-day (0–1440), or None if unparseable."""
    try:
        h, m = str(s).split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v <= 24 * 60 else None
    except (ValueError, AttributeError):
        return None


def _time_in_window(minute: int, start_min: int, end_min: int) -> bool:
    """Is minute-of-day inside [start, end)? Handles windows crossing midnight
    (start > end, e.g. 23:30→06:30). start == end means the whole day."""
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= minute < end_min
    return minute >= start_min or minute < end_min


def _match_band(bands: list, weekday: int, minute: int):
    """First band that covers this (weekday, minute-of-day). A band crossing midnight
    (start > end, e.g. 23:30→07:30) is anchored to the day it STARTS: its pre-midnight
    part [start,24:00) applies when that day is in `days`; its post-midnight part
    [00:00,end) belongs to the PREVIOUS day's membership — so a Saturday-only off-peak
    band also covers the early Sunday hours, but a Sunday-only band does not. (This fixes
    day-restricted midnight-crossing bands, which previously dropped the after-midnight
    hours to the base price.)"""
    for b in bands:
        days = b.get("days")
        if not isinstance(days, list) or not days:
            days = list(range(7))
        s, e = _parse_hhmm(b.get("start")), _parse_hhmm(b.get("end"))
        if s is None or e is None:
            continue
        if s == e:                                        # whole-day band
            if weekday in days:
                return b
        elif s < e:                                       # same-day window
            if s <= minute < e and weekday in days:
                return b
        else:                                             # crosses midnight
            if minute >= s and weekday in days:           # pre-midnight → this day
                return b
            if minute < e and (weekday - 1) % 7 in days:  # post-midnight → previous day
                return b
    return None


def _resolve_price(band, ctype: str, base: float, base_set: bool):
    """Price for a charge type at a moment: the band's per-type price if set, else
    the base price. is_set=False means neither provides a real price (→ not costed)."""
    if band is not None:
        bp = (band.get("prices") or {}).get(ctype)
        if bp is not None:
            return float(bp), True
    return base, base_set


def _next_charge_start_utc(db, started_at) -> Optional[str]:
    """UTC start of the first charge beginning strictly after `started_at` (a raw stored
    value), or None. Used to cap a charge's power-sample window: an orphan/overlapping
    charge whose ended_at bled past a later charge (see the poller's close_orphan_charges)
    must NOT absorb the next charge's power samples into its own window or cost."""
    try:
        row = db.execute(
            "SELECT MIN(started_at) AS s FROM charges WHERE started_at > ?", (started_at,)
        ).fetchone()
    except sqlite3.Error:
        return None   # no charges table (isolated unit tests) → no cap
    return _iso_to_utc(row["s"]) if (row and row["s"]) else None


def _power_window_bounds(db, started_at, ended_at):
    """(lower_utc, upper, upper_is_exclusive) for a charge's charging=1 samples, capping
    the upper bound at the next charge's start so a window/cost never leaks across charges.
    When capped, the upper bound is EXCLUSIVE (the next charge owns samples at its start)."""
    lo = _iso_to_utc(started_at) or started_at
    hi = _iso_to_utc(ended_at) or lo
    nxt = _next_charge_start_utc(db, started_at)
    if nxt and nxt <= hi:
        return lo, nxt, True
    return lo, hi, False


def compute_cost(charge, config: Optional[dict] = None, ac_kwh: Optional[float] = None):
    """Cost for ONE charge using the pricing config in effect *now*. This is the
    single place a charge's cost is set, and it is frozen afterwards (no retroactive
    recompute when prices/bands change later). Returns a float (0.0 = free) or None
    when the type/price isn't known yet.
        flat        → energy × base price for the charge's type
        TOU 'start' → price of the band matching the start day+time (else base)
        TOU 'split' → energy split across bands by the real power curve, each
                      sample priced by the band matching its own day+time

    `ac_kwh`: for HOME charges on a configured wallbox, the caller passes the real AC energy the
    wallbox delivered (what you actually pay the utility, incl. AC→DC conversion losses). When given
    (>0) it replaces the DC SOC-energy as the billed amount; otherwise we bill the DC energy (the only
    figure we have for public/away charges). The band-weighting (timing) is unchanged — AC and DC flow
    at the same times — so only the total energy differs.
    """
    location_type = charge["location_type"]
    # `ac_kwh` (when given) is the wallbox energy the poller MEASURED for this charge — the counter
    # delta start→stop, an exact figure, not an estimate. HOME charges are billed on it; everything
    # else (and HOME without a wallbox) on the battery (DC/SoC) energy. The caller picks which.
    energy = ac_kwh if (ac_kwh and ac_kwh > 0) else (charge["energy_added_kwh"] or 0)
    if not location_type or energy <= 0:
        return None
    if location_type == "FREE":
        return 0.0

    if config is None:
        config = get_cost_config()
    prices = get_charge_prices()
    key = PRICE_KEYS.get(location_type, "")
    base_set = key in prices
    base = float(prices.get(key, 0.0) or 0.0)

    bands = config.get("bands") or []
    if config.get("mode") != "tou" or not bands:
        return round(energy * base, 2) if base else None

    def _start_band_cost():
        dt = _local_dt(charge["started_at"])
        if dt is None:
            return round(energy * base, 2) if base else None
        band = _match_band(bands, dt.weekday(), dt.hour * 60 + dt.minute)
        price, is_set = _resolve_price(band, location_type, base, base_set)
        if not is_set and price == 0:
            return None
        return round(energy * price, 2)

    if config.get("method") == "start":
        return _start_band_cost()

    # An in-progress charge (no ended_at) has no integrable curve yet → price by start band.
    if not charge["ended_at"]:
        return _start_band_cost()

    # method 'split': integrate the power curve, price each interval by its band. The window
    # is capped at the next charge's start so an orphan/overlapping charge can't integrate a
    # later charge's power (which would also distort the band weighting).
    db = _get()
    lo, hi, excl = _power_window_bounds(db, charge["started_at"], charge["ended_at"])
    rows = db.execute(
        "SELECT recorded_at, charge_voltage_v, charge_current_a FROM positions "
        "WHERE charging = 1 AND recorded_at >= ? AND recorded_at " + ("<" if excl else "<=")
        + " ? ORDER BY recorded_at",
        (lo, hi),
    ).fetchall()
    samples = []
    for r in rows:
        dt = _local_dt(r["recorded_at"])
        if dt is not None:
            power = abs((r["charge_voltage_v"] or 0) * (r["charge_current_a"] or 0)) / 1000.0
            samples.append((dt, power))

    total_e, weighted, any_set = 0.0, 0.0, False
    for (dt0, p0), (dt1, p1) in zip(samples, samples[1:]):
        hours = (dt1 - dt0).total_seconds() / 3600.0
        if hours <= 0 or hours > 0.25:   # skip non-positive AND multi-hour gaps (charger
            continue                     # paused / poll miss): never price a phantom interval
                                         # across the gap (mirrors _integrate_charge_energy_kwh)
        e = (p0 + p1) / 2.0 * hours
        if e <= 0:
            continue
        band = _match_band(bands, dt0.weekday(), dt0.hour * 60 + dt0.minute)
        price, is_set = _resolve_price(band, location_type, base, base_set)
        any_set = any_set or is_set
        total_e += e
        weighted += e * price

    if total_e <= 0:               # no usable curve → fall back to the start band
        return _start_band_cost()
    if not any_set and weighted == 0:
        return None
    # scale the time-weighted average price onto the authoritative (SOC) energy,
    # so the total stays consistent with the energy shown elsewhere.
    return round(energy * (weighted / total_e), 2)


def update_charge_type(charge_id: int, location_type: str,
                       manual_cost: Optional[float] = None) -> dict:
    """Set location_type and (re)compute the cost from the pricing config in effect now (flat or
    time-of-use). Frozen afterwards (the 'new charges only' rule). HOME charges are billed on the
    wallbox energy the POLLER measured at charge start/stop (charges.ac_energy_kwh = the counter
    delta — exact, not estimated) when available; otherwise, and for every other type, on the
    battery (DC/SoC) energy.

    `MANUAL` is the user-entered total actually paid (the public-charging jungle — subscriptions,
    session/idle fees, pay-method rates — can't be modelled by a per-kWh tariff). It OVERRIDES the
    automatic cost: `manual_cost` is stored verbatim and the automatic costers (auto-confirm and the
    one-time repairs) leave a MANUAL charge's cost alone. It still feeds the WAC like any priced
    charge (rate = cost ÷ billed DC energy)."""
    db = _conn_rw()
    row = db.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()
    if not row:
        return {}

    charge = dict(row)
    charge["location_type"] = location_type
    if location_type == "MANUAL":
        # Keep the existing cost if no amount was supplied (e.g. re-tagging without re-typing it).
        cost = round(manual_cost, 2) if manual_cost is not None else charge.get("cost")
    else:
        meter = charge.get("ac_energy_kwh")
        billed = meter if (location_type == "HOME" and meter and meter > 0) else None
        cost = compute_cost(charge, ac_kwh=billed)

    db.execute(
        "UPDATE charges SET location_type=?, cost=? WHERE id=?",
        (location_type, cost, charge_id)
    )
    db.commit()
    return dict(db.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone())


def auto_confirm_home_charges() -> int:
    """Auto-assign HOME to closed, still-untyped charges where the wallbox measured real AC
    energy (opt-in `wallbox_auto_home` setting; idea credit: @hubcasale, PR #47): if YOUR
    wallbox saw energy flow during the session, the charge happened at home. DC/public
    charges and reconstructed ones carry no wallbox session energy, so they stay manual.
    Each hit goes through update_charge_type — the SAME path as a manual badge confirm —
    so the cost honours the pricing config (flat or TOU bands) and the AC-energy billing;
    the type stays user-editable afterwards. The 0.05 kWh floor mirrors the phantom-charge
    threshold (meter jitter must not tag a charge). Runs on page renders (a settings probe
    + one SELECT, normally 0 rows) and when the toggle is switched on; returns # confirmed."""
    try:
        if get_setting("wallbox_auto_home", "0") != "1":
            return 0
        rows = _get().execute(
            "SELECT id FROM charges WHERE location_type IS NULL AND ended_at IS NOT NULL "
            "AND COALESCE(reconstructed, 0) = 0 AND COALESCE(ac_energy_kwh, 0) > 0.05"
        ).fetchall()
    except sqlite3.Error:   # fresh install — settings/charges tables not created yet
        return 0
    for r in rows:
        update_charge_type(r["id"], "HOME")
    return len(rows)


# ── 📍 charging-station labels (resolved by web/charger_locator.py) ───────────
# A candidate is a closed public charge with a GPS fix and no label yet. Home charges
# are excluded twice over — by the HOME type and by any wallbox session evidence — so a
# pure-home install never triggers a single network lookup.
_LOCATION_CANDIDATES_WHERE = (
    "ended_at IS NOT NULL AND location_name IS NULL "
    "AND latitude IS NOT NULL AND longitude IS NOT NULL "
    "AND latitude <> 0 AND longitude <> 0 "
    "AND COALESCE(location_type, '') <> 'HOME' "
    "AND wallbox_energy_start_kwh IS NULL AND COALESCE(ac_energy_kwh, 0) <= 0.05"
)


def has_location_lookup_candidates() -> bool:
    try:
        return _get().execute(
            f"SELECT 1 FROM charges WHERE {_LOCATION_CANDIDATES_WHERE} LIMIT 1"
        ).fetchone() is not None
    except sqlite3.Error:  # fresh install — column not migrated by the poller yet
        return False


def get_location_lookup_candidates(limit: int = 40) -> list[dict]:
    try:
        rows = _get().execute(
            f"SELECT id, latitude, longitude FROM charges WHERE {_LOCATION_CANDIDATES_WHERE} "
            "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def get_labelled_locations() -> list[tuple]:
    """(lat, lon, label) of every already-resolved charge — '' sentinels included — so
    a charge at an already-known spot reuses the answer instead of re-asking Overpass."""
    try:
        rows = _get().execute(
            "SELECT latitude, longitude, location_name FROM charges "
            "WHERE location_name IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(r["latitude"], r["longitude"], r["location_name"]) for r in rows]


def set_charge_location_name(charge_id: int, name: str) -> None:
    db = _conn_rw()
    db.execute("UPDATE charges SET location_name=? WHERE id=?", (name, charge_id))
    db.commit()


def update_charge_price(key: str, value: float) -> None:
    """Persist a base €/kWh price. Per the 'new charges only' rule, this does NOT
    retroactively recompute already-recorded charges: a charge's cost is frozen
    when its type is confirmed, and only charges confirmed from here on use the
    new price. Same goes for time-of-use band/mode edits."""
    set_setting(key, str(value))


def upsert_vehicle(vin: str, car_type: str) -> None:
    """Pre-populate vehicles table from setup wizard (before first poller run)."""
    db = _conn_rw()
    db.execute(
        "INSERT OR IGNORE INTO vehicles (vin, car_type) VALUES (?,?)",
        (vin, car_type),
    )
    db.execute("UPDATE vehicles SET car_type=? WHERE vin=?", (car_type, vin))
    db.commit()


def get_vehicle():
    db = _get()
    v = db.execute("SELECT * FROM vehicles LIMIT 1").fetchone()
    s = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
    return dict(v) if v else None, s


def clear_optimistic_status() -> None:
    """Remove the in-memory optimistic overlay (called when API does not confirm the command)."""
    global _opt_overrides, _opt_expiry
    _opt_overrides = {}
    _opt_expiry = 0.0


def extend_optimistic_status() -> None:
    """Re-arm the optimistic overlay's TTL while a command is still being verified.
    The post-command verification can poll the cloud for up to ~30s waiting for the
    car's state to propagate; without this the overlay would expire mid-wait and the
    UI would briefly flash the stale pre-command state (GitHub #34)."""
    global _opt_expiry
    if _opt_overrides:
        _opt_expiry = time.time() + _OPT_TTL


def write_optimistic_status(overrides: dict) -> None:
    """Copy the latest position row, apply field overrides, insert as new row.
       Also caches overrides in memory so get_latest_status() can re-apply them
       even if the poller overwrites the DB row before the UI refresh fires.
    """
    global _opt_overrides, _opt_expiry
    db = _conn_rw()
    row = db.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return
    d = dict(row)
    d.pop("id")
    d["recorded_at"] = datetime.now(timezone.utc).isoformat()
    d.update(overrides)
    cols = ", ".join(d.keys())
    placeholders = ", ".join("?" for _ in d)
    db.execute(f"INSERT INTO positions ({cols}) VALUES ({placeholders})", list(d.values()))
    db.commit()
    _opt_overrides = dict(overrides)
    _opt_expiry = time.time() + _OPT_TTL


def save_fresh_signals(signals: dict) -> None:
    """Write a fresh position row from raw API signals (called after a command)."""
    db = _conn_rw()
    v = db.execute("SELECT id FROM vehicles LIMIT 1").fetchone()
    if not v:
        return
    vehicle_id = v["id"]

    def sig(key, default=0):  return int(signals.get(key) or default)
    def sigf(key, default=0.0): return float(signals.get(key) or default)

    def _is_charging() -> bool:
        """Charging only happens while PARKED, so the car must be stationary (gear P,
        speed ~0); plus the cable plugged in (1149) AND a real charge current (1178). The
        motion gate is essential: during regen the pack current is strongly negative (same
        sign as charging) and 1149 reads 1 spuriously, so without it driving is mistaken
        for charging. Signal 1939 (AC fan mode) is not used."""
        if int(signals.get("1010") or 0) != 0:   # gear R/N/D → moving
            return False
        try:
            if float(signals.get("1319") or 0) > 2.0:   # speed > 2 km/h → moving
                return False
        except (TypeError, ValueError):
            pass
        if int(signals.get("1149") or 0) == 0:
            return False
        cur = signals.get("1178"); volt = signals.get("1177"); rem = signals.get("1200")
        try:    cur = float(cur) if cur is not None else None
        except (TypeError, ValueError): cur = None
        try:    volt = float(volt) if volt is not None else None
        except (TypeError, ValueError): volt = None
        power = abs(cur * volt) / 1000.0 if (cur is not None and volt is not None and abs(cur) >= 3.0) else None
        if cur is not None:
            if abs(cur) < 3.0:
                return False
            return rem is not None or (power is not None and power >= 1.0)
        if power is not None:
            return power >= 1.0 and rem is not None
        return int(signals.get("1149") or 0) == 2

    gear_map = {0: "P", 1: "R", 2: "N", 3: "D"}
    # Windows: flag OR position % (the T03 reports only the %, the B10 only the flag) — same shared
    # logic as the Vehicle page so the Overview tile / Commands grid agree with it (#62). use_pct is
    # gated by the capability profile, exactly as _parse_vehicle_status does.
    _wvin = (get_vehicle()[0] or {}).get("vin")
    _wstates = capability_profile.window_open_states(
        signals, bool(_wvin) and capability_profile.is_shown(_wvin, "windows_pct"))
    windows_open = int(any(_wstates))
    windows_open_count = sum(1 for w in _wstates if w)

    # Plug from signal 1149 (charge connection status), gated by motion. Signal 47
    # (acInputSlowCharge) latches at 1 for ~5 min after an AC charge on the B10 and does
    # NOT clear on unplug, so it cannot drive session-close; 1149 drops to 0 immediately.
    # 1149 reads 1 spuriously during regen at speed → suppress while moving (mirrors
    # _is_charging). 47 is only a fallback when 1149 is absent. See poller/client._is_plugged_in.
    def _is_plugged() -> bool:
        if int(signals.get("1010") or 0) != 0:          # gear R/N/D → moving
            return False
        try:
            if float(signals.get("1319") or 0) > 2.0:   # speed > 2 km/h → moving
                return False
        except (TypeError, ValueError):
            pass
        conn = signals.get("1149")
        if conn is None:
            return int(signals.get("47") or 0) == 1     # legacy fallback when 1149 absent
        try:
            return int(conn) in (1, 2)
        except (TypeError, ValueError):
            return False
    plug_connected = _is_plugged()

    db.execute(
        """INSERT INTO positions (
            vehicle_id, recorded_at,
            latitude, longitude, speed_kmh, odometer_km,
            soc, range_km, gear, charging,
            battery_min_temp, climate_target_temp, inside_temp,
            is_locked, climate_on, plug_connected,
            climate_cooling, climate_heating, climate_defrost,
            trunk_open, windows_open, sunshade_open,
            remaining_charge_min, charge_voltage_v, charge_current_a, charge_completed, security_active,
            windows_open_count,
            door_driver_open, door_passenger_open, door_rear_left_open, door_rear_right_open,
            window_fl_open, window_rl_open
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            vehicle_id,
            datetime.now(timezone.utc).isoformat(),
            sigf("3725") or sigf("2190"),
            sigf("3724") or sigf("2191"),
            sigf("1319"), sigf("1318"),
            sigf("100003") or sigf("1204"),
            sigf("3260"),
            gear_map.get(sig("1010"), "P"),
            int(_is_charging()),
            sigf("1182"), sigf("2183"), sigf("1349"),
            sig("1298"), sig("1938"), int(plug_connected),
            int(sig("2669") == 2), int(sig("2681") == 2), int(sig("1945") == 2),
            sig("1281"), windows_open, sig("1724"),
            sig("1200") or None,
            sigf("1177") or None,
            sigf("1178") or None,
            int(int(signals.get("3736") or 0) != 0),
            int(int(signals.get("1255") or 0) != 0),
            windows_open_count,
            1 if sig("1277") else 0, 1 if sig("1278") else 0,
            1 if sig("1279") else 0, 1 if sig("1280") else 0,
            1 if _wstates[0] else 0, 1 if _wstates[2] else 0,
        ),
    )
    db.commit()


def get_latest_status() -> Optional[dict]:
    db = _get()
    row = db.execute(
        "SELECT * FROM positions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    # Apply in-memory optimistic overrides if still within TTL
    if time.time() < _opt_expiry and _opt_overrides:
        d.update(_opt_overrides)
    # GPS fallback: a poll can come back with no fix → (0,0). Don't let that blank the Overview
    # map (or reset Navigation's start point) — fall back to the last position that had a real
    # fix and flag it stale, so the last known location keeps showing. Only a true (0,0)/null is
    # treated as "no fix" (a car genuinely on the prime meridian at lon 0 is kept).
    _lat, _lon = d.get("latitude"), d.get("longitude")
    if _lat is None or _lon is None or (abs(_lat) < 1e-6 and abs(_lon) < 1e-6):
        last = db.execute(
            "SELECT latitude, longitude FROM positions "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND NOT (ABS(latitude) < 1e-6 AND ABS(longitude) < 1e-6) "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            d["latitude"], d["longitude"] = last["latitude"], last["longitude"]
            d["position_stale"] = True
    # Charge power: positions stores current/voltage, not a power column. Compute it
    # (|I×V|), only when the charge current is meaningful (>=3A). Signal 49 is NOT a
    # power (it's the left-mirror-heating flag) and must never be used here.
    cur_a = d.get("charge_current_a")
    volt_v = d.get("charge_voltage_v")
    if cur_a is not None and volt_v is not None and abs(cur_a) >= 3.0:
        d["charge_power_kw"] = round(abs(cur_a * volt_v) / 1000.0, 2)
    else:
        d["charge_power_kw"] = 0.0
    # Derived "ventilating" = climate on but neither cooling, heating nor defrosting (wind mode).
    d["climate_venting"] = bool(d.get("climate_on")) and not d.get("climate_cooling") \
        and not d.get("climate_heating") and not d.get("climate_defrost")
    # How long ago
    try:
        ts = datetime.fromisoformat(d["recorded_at"])
        now = datetime.now(timezone.utc)
        delta = int((now - ts).total_seconds())
        if delta < 60:
            d["last_seen"] = f"{delta}s ago"
        elif delta < 3600:
            d["last_seen"] = f"{delta // 60}m ago"
        else:
            d["last_seen"] = f"{delta // 3600}h ago"
    except Exception:
        d["last_seen"] = "unknown"
    # OTA / software-update status (the poller scans the account message inbox for an update notice).
    d["ota"] = get_ota_status()
    return d


def get_ota_status() -> dict:
    """OTA / software-update status the poller stored (from scanning the account inbox). Returns
    {available:bool, title:str|None, time:str|None (localized "dd/mm HH:MM")}. False until the
    poller has run a check; only ever True when an update notice is actually present."""
    available = get_setting("ota_available", "") == "1"
    title = get_setting("ota_title", "") or None
    when = None
    raw = get_setting("ota_time", "")
    if raw:
        try:
            dt = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
            when = (_local_dt(dt.isoformat()) or dt).strftime("%d/%m %H:%M")
        except (TypeError, ValueError, OSError):
            when = None
    return {"available": available, "title": title, "time": when}


def delete_trip(trip_id: int) -> bool:
    """Permanently remove a trip and its GPS track. Returns True if a trip was deleted.
    Day/month/lifetime trip totals recompute from the DB, so they update automatically."""
    db = _conn_rw()
    # Deleting a merged trip removes the whole group (the parent + every child) and their tracks.
    ids = [trip_id] + [r["id"] for r in db.execute(
        "SELECT id FROM trips WHERE merged_into_id=?", (trip_id,)).fetchall()]
    ph = ",".join("?" * len(ids))
    cur = db.execute(f"DELETE FROM trips WHERE id IN ({ph})", ids)
    db.execute(f"DELETE FROM trip_positions WHERE trip_id IN ({ph})", ids)
    db.commit()
    return cur.rowcount > 0


def delete_charge(charge_id: int) -> bool:
    """Permanently remove a charge session. Returns True if one was deleted. Day/month/lifetime
    charge totals recompute from the DB automatically. The shared per-poll positions log is untouched."""
    db = _conn_rw()
    cur = db.execute("DELETE FROM charges WHERE id=?", (charge_id,))
    db.commit()
    return cur.rowcount > 0


# ── Command responsiveness log (car↔cloud reachability proxy) ────────────────
# A remote command is the ONLY moment Mate talks to the car in real time — polls just read
# the cloud's CACHED state, so they succeed even when the car has weak coverage. Logging each
# command's outcome therefore measures how responsive the car itself is (a proxy for the
# cellular coverage where it's parked) — which is exactly what a "cloud OK but car didn't
# confirm" timeout is telling us. This is why one user can see timeouts while everyone else is fine.
def _ensure_command_log(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS command_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
        "action TEXT, outcome TEXT NOT NULL, latency_ms INTEGER)")


def log_command(action: str, outcome: str, latency_ms: Optional[int] = None) -> None:
    """Record one remote-command outcome (confirmed|timeout_car|cloud_unreachable|rejected).
    Best-effort: never raises into the command path. Keeps ~90 days."""
    try:
        db = _conn_rw()
        _ensure_command_log(db)
        db.execute("INSERT INTO command_log (ts, action, outcome, latency_ms) VALUES (?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), action, outcome, latency_ms))
        db.execute("DELETE FROM command_log WHERE ts < ?",
                   ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),))
        db.commit()
    except Exception:
        pass


def command_responsiveness(last_n: int = 24, min_samples: int = 3) -> dict:
    """How reliably the car answers commands — a proxy for its cellular coverage. Window is by
    COUNT (the LAST `last_n` commands), NOT by time: it stays visible between command sessions
    and recovers to green within ~last_n good commands (old timeouts scroll out). Only
    'confirmed' vs 'timeout_car' count (a cloud/network or auth failure isn't the car's fault).
    ALWAYS returns a dict so the badge stays visible — state='unknown' until min_samples commands."""
    rows = []
    try:
        db = _conn_rw()
        _ensure_command_log(db)
        rows = db.execute(
            "SELECT outcome, latency_ms FROM command_log "
            "WHERE outcome IN ('confirmed','timeout_car') ORDER BY id DESC LIMIT ?",
            (last_n,)).fetchall()
    except Exception:
        rows = []
    total = len(rows)
    if total < min_samples:
        return {"state": "unknown", "confirmed": 0, "timeouts": 0, "total": total,
                "rate": None, "last_n": last_n, "avg_latency_ms": None}
    confirmed = sum(1 for r in rows if r["outcome"] == "confirmed")
    lat = [r["latency_ms"] for r in rows
           if r["outcome"] == "confirmed" and r["latency_ms"] is not None]
    rate = confirmed / total
    state = ("responsive" if rate >= 0.8 else
             "intermittent" if rate >= 0.4 else "unresponsive")
    return {"state": state, "confirmed": confirmed, "timeouts": total - confirmed,
            "total": total, "rate": round(rate, 2), "last_n": last_n,
            "avg_latency_ms": int(sum(lat) / len(lat)) if lat else None}


# ── Manual trip merge (reversible) ──────────────────────────────────────────────
# A merged trip is a parent + child trips (merged_into_id = parent.id), joined by the user when
# a journey was split by a SHORT, NON-charging stop. Nothing is deleted or overwritten — the group
# stats are computed on the fly, so "unmerge" restores the originals exactly.
TRIP_MERGE_GAP_DEFAULT = 30   # minutes
TRIP_MERGE_GAP_MIN = 5
TRIP_MERGE_GAP_MAX = 90


def _gap_minutes(end_iso, start_iso):
    """Minutes from end_iso to start_iso (raw stored UTC ISO). None if unparseable."""
    try:
        return (datetime.fromisoformat(start_iso) - datetime.fromisoformat(end_iso)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _children_by_parent(db) -> dict:
    """All merged child trips grouped by parent id (one query)."""
    out: dict = {}
    for r in db.execute("SELECT * FROM trips WHERE merged_into_id IS NOT NULL").fetchall():
        out.setdefault(r["merged_into_id"], []).append(dict(r))
    return out


def _segment_ids(db, trip_id: int) -> list:
    """Every trip id in the merge-group containing trip_id (parent + children); [trip_id] if none."""
    row = db.execute("SELECT id, merged_into_id FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not row:
        return [trip_id]
    parent = row["merged_into_id"] or row["id"]
    return [parent] + [r["id"] for r in
            db.execute("SELECT id FROM trips WHERE merged_into_id=?", (parent,)).fetchall()]


def _trip_group_stats(parent: dict, children: list) -> dict:
    """Parent dict enriched with the combined stats of [parent + children] (earliest start →
    latest end). Pure display math — stored rows are untouched. The merge guard guarantees no
    charge in any gap, so the SoC delta (energy/efficiency) stays valid."""
    d = dict(parent)
    d["merged_count"] = 1
    d["is_merged"] = False
    if not children:
        return d
    segs = sorted([parent, *children], key=lambda t: t.get("started_at") or "")
    first, last = segs[0], segs[-1]
    d["started_at"], d["start_soc"] = first.get("started_at"), first.get("start_soc")
    d["start_odometer_km"] = first.get("start_odometer_km")
    d["start_lat"], d["start_lon"] = first.get("start_lat"), first.get("start_lon")
    d["ended_at"], d["end_soc"] = last.get("ended_at"), last.get("end_soc")
    d["end_odometer_km"] = last.get("end_odometer_km")
    d["end_lat"], d["end_lon"] = last.get("end_lat"), last.get("end_lon")
    so, eo = first.get("start_odometer_km"), last.get("end_odometer_km")
    if so is not None and eo is not None and eo >= so and so > 0:
        d["distance_km"] = round(eo - so, 2)
    else:
        d["distance_km"] = round(sum((s.get("distance_km") or 0) for s in segs), 2)
    d["duration_min"] = round(sum((s.get("duration_min") or 0) for s in segs), 1)   # DRIVING only
    d["regen_kwh"] = round(sum((s.get("regen_kwh") or 0) for s in segs), 3)
    ssoc, esoc, dist = d["start_soc"], d["end_soc"], d.get("distance_km") or 0
    if ssoc is not None and esoc is not None and dist > 0:
        energy = max((ssoc - esoc) / 100.0 * get_battery_capacity_kwh(), 0)
        d["efficiency_kwh_100km"] = round(energy / dist * 100, 1) if energy > 0 else None
    d["merged_count"] = len(segs)
    d["is_merged"] = True
    d["segment_ids"] = [s["id"] for s in segs]
    return d


def get_mergeable_pairs(gap_min: int = TRIP_MERGE_GAP_DEFAULT) -> list:
    """Eligible adjacent top-level trip pairs for the merge UI: B starts within gap_min of A's
    (group) end AND B's start SoC is not higher than A's end SoC (a SoC rise = a charge in the
    gap → never mergeable). Returns [{a_id, b_id, gap_min}]."""
    db = _get()
    kids = _children_by_parent(db)
    tops = [dict(r) for r in db.execute(
        "SELECT * FROM trips WHERE merged_into_id IS NULL AND ended_at IS NOT NULL "
        "ORDER BY started_at").fetchall()]
    groups = [_trip_group_stats(t, kids.get(t["id"], [])) for t in tops]
    pairs = []
    for a, b in zip(groups, groups[1:]):
        gap = _gap_minutes(a.get("ended_at"), b.get("started_at"))
        if gap is None or gap < 0 or gap >= gap_min:
            continue
        if (a.get("end_soc") is not None and b.get("start_soc") is not None
                and b["start_soc"] > a["end_soc"]):
            continue   # SoC rose → charged in the gap
        pairs.append({"a_id": a["id"], "b_id": b["id"], "gap_min": round(gap)})
    return pairs


def merge_trips(parent_id: int, child_id: int, gap_min: int = TRIP_MERGE_GAP_DEFAULT) -> dict:
    """Merge child into parent (the earlier of the two becomes the parent). Re-validates the
    eligibility server-side. Reversible: only sets merged_into_id, nothing is overwritten."""
    db = _conn_rw()
    a = db.execute("SELECT * FROM trips WHERE id=? AND merged_into_id IS NULL", (parent_id,)).fetchone()
    b = db.execute("SELECT * FROM trips WHERE id=? AND merged_into_id IS NULL", (child_id,)).fetchone()
    if not a or not b:
        return {"ok": False, "error": "not_found_or_already_merged"}
    a, b = dict(a), dict(b)
    if (a.get("started_at") or "") > (b.get("started_at") or ""):
        a, b = b, a                                   # parent = earlier trip
    kids = _children_by_parent(db)
    a_grp = _trip_group_stats(a, kids.get(a["id"], []))
    gap = _gap_minutes(a_grp.get("ended_at"), b.get("started_at"))
    if gap is None or gap < 0 or gap >= gap_min:
        return {"ok": False, "error": "gap_too_large"}
    if (a_grp.get("end_soc") is not None and b.get("start_soc") is not None
            and b["start_soc"] > a_grp["end_soc"]):
        return {"ok": False, "error": "soc_rose_charge_in_gap"}
    # absorb B and any of B's own children into A (flatten the chain so all point to A)
    db.execute("UPDATE trips SET merged_into_id=? WHERE id=? OR merged_into_id=?",
               (a["id"], b["id"], b["id"]))
    db.commit()
    return {"ok": True, "parent_id": a["id"]}


def unmerge_trip(parent_id: int) -> dict:
    """Split a merged group back into its original trips — clears merged_into_id on every child.
    All rows were untouched, so they reappear exactly as before."""
    db = _conn_rw()
    cur = db.execute("UPDATE trips SET merged_into_id=NULL WHERE merged_into_id=?", (parent_id,))
    db.commit()
    return {"ok": True, "restored": cur.rowcount}


def preview_merge(parent_id: int, child_id: int) -> Optional[dict]:
    """Group stats the merge WOULD produce (for the confirm dialog), without committing."""
    db = _get()
    a = db.execute("SELECT * FROM trips WHERE id=?", (parent_id,)).fetchone()
    b = db.execute("SELECT * FROM trips WHERE id=?", (child_id,)).fetchone()
    if not a or not b:
        return None
    a, b = dict(a), dict(b)
    if (a.get("started_at") or "") > (b.get("started_at") or ""):
        a, b = b, a
    kids = _children_by_parent(db)
    children = kids.get(a["id"], []) + [b] + kids.get(b["id"], [])
    g = _trip_group_stats(a, children)
    drive = g.get("duration_min") or 0
    elapsed = _gap_minutes(g.get("started_at"), g.get("ended_at"))
    g["stop_min"] = round(max(elapsed - drive, 0)) if elapsed is not None else None
    g["started_at"] = _local_iso(g.get("started_at"))
    g["ended_at"] = _local_iso(g.get("ended_at"))
    return g


def get_merge_preview_route(a_id: int, b_id: int, max_points: int = 120) -> list[dict]:
    """Downsampled union GPS track of the two trips' groups — for the merge-preview thumbnail."""
    db = _get()
    ids = list(dict.fromkeys(_segment_ids(db, a_id) + _segment_ids(db, b_id)))
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT latitude, longitude FROM trip_positions WHERE trip_id IN ({ph}) "
        "AND latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY recorded_at, id", ids).fetchall()
    pts = [dict(r) for r in rows]
    if len(pts) <= max_points:
        return pts
    step = len(pts) / max_points
    out = [pts[int(i * step)] for i in range(max_points)]
    out[-1] = pts[-1]
    return out


def get_trips(limit: int = 500) -> list[dict]:
    db = _get()
    kids = _children_by_parent(db)
    rows = db.execute(
        """SELECT * FROM trips
           WHERE ended_at IS NOT NULL AND merged_into_id IS NULL
           ORDER BY started_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_trip_group_stats(dict(r), kids.get(r["id"], [])) for r in rows]


def get_trips_grouped() -> list[dict]:
    """Return trips nested as year → month → day for the sidebar tree view."""
    trips = get_trips()
    from collections import OrderedDict

    def _node(label):
        return {"label": label, "km": 0, "count": 0,
                "_eff_wsum": 0.0, "_eff_wdist": 0.0, "avg_eff": None}

    def _add(node, km, eff):
        node["km"]    = round(node["km"] + km, 2)
        node["count"] += 1
        if eff and km > 0:
            node["_eff_wsum"]  += km * eff
            node["_eff_wdist"] += km

    def _finalize(node):
        if node["_eff_wdist"] > 0:
            node["avg_eff"] = round(node["_eff_wsum"] / node["_eff_wdist"], 1)

    lang = get_language()
    years: dict = OrderedDict()
    for t in trips:
        if not t.get("started_at"):
            continue
        dt = _local_dt(t["started_at"])
        if dt is None:
            continue
        # Rewrite to local-time ISO so the template (started_at[11:16]) shows local
        t["started_at"] = dt.isoformat()
        t["ended_at"] = _local_iso(t.get("ended_at"))

        yr  = dt.strftime("%Y")
        mo  = i18n.fmt_month_year(lang, dt)
        day = i18n.fmt_day_month_year(lang, dt)

        years.setdefault(yr, {**_node(yr), "months": OrderedDict()})
        years[yr]["months"].setdefault(mo, {**_node(mo), "days": OrderedDict()})
        years[yr]["months"][mo]["days"].setdefault(day, {**_node(day), "trips": []})

        years[yr]["months"][mo]["days"][day]["trips"].append(t)

        km  = t.get("distance_km") or 0
        eff = t.get("efficiency_kwh_100km")
        for node in [years[yr], years[yr]["months"][mo], years[yr]["months"][mo]["days"][day]]:
            _add(node, km, eff)

    # Compute weighted avg efficiency for every node
    for yr_node in years.values():
        _finalize(yr_node)
        for mo_node in yr_node["months"].values():
            _finalize(mo_node)
            for day_node in mo_node["days"].values():
                _finalize(day_node)

    return list(years.values())


def get_trips_summary() -> dict:
    """Grand totals for the trips dashboard hero (no extra polling — pure SQL).

    Values are returned RAW, with no rounding — the template decides how to
    display them. avg_eff is a weighted mean (an inherently fractional ratio)."""
    db = _get()
    r = db.execute(
        """SELECT SUM(CASE WHEN merged_into_id IS NULL THEN 1 ELSE 0 END) AS n,
                  COALESCE(SUM(distance_km), 0)              AS km,
                  COALESCE(SUM(regen_kwh), 0)                AS regen,
                  SUM(distance_km * efficiency_kwh_100km)    AS eff_wsum,
                  SUM(CASE WHEN efficiency_kwh_100km IS NOT NULL
                           THEN distance_km END)             AS eff_wdist
           FROM trips WHERE ended_at IS NOT NULL"""
    ).fetchone()
    return {
        "count":    r["n"],
        "km":       r["km"] or 0,
        "regen":    r["regen"] or 0,
        "avg_eff":  (r["eff_wsum"] / r["eff_wdist"]) if r["eff_wdist"] else None,
    }


def _wac_blend(charges) -> Optional[float]:
    """Weighted-average-cost blended €/kWh of the battery after a chronological list of PRICED
    charges (GitHub #53). Pure (no DB) so it's simulation/unit-testable: each item is a dict with
    start_soc, end_soc, cost, ac_energy_kwh, location_type, energy_added_kwh.

    Model: the battery is ONE reservoir at a blended price; only a charge moves the price,
    consumption never does → replay the charges, anchoring the mix on each charge's SoC. Capacity
    CANCELS out (SoC ratios), so this is capacity-free and robust to SoH error. Update per charge:

        p' = (start_soc·p + (end_soc − start_soc)·rate) / end_soc

    where rate = charge cost ÷ its billed energy (_billed_kwh: wallbox AC for HOME, else battery DC —
    same basis as the per-charge € and the #51 trip-rate fix). Bootstrap: the first priced charge
    sets p to its own rate (the pre-existing energy is valued at the first thing we can measure).
    Unconfirmed charges (cost=NULL) are simply ABSENT from this list → carry-forward, i.e. the blend
    is unchanged across them — Mate's framework rule "no cost until confirmed, HOME excluded"."""
    p = None
    for c in charges:
        ss, es = c.get("start_soc"), c.get("end_soc")
        if ss is None or es is None or es <= 0 or es <= ss:
            continue                         # need a real SoC rise to weight the mix
        basis = _billed_kwh(c)
        cost = c.get("cost")
        if cost is None or not basis or basis <= 0:
            continue                         # unpriced → must not move the blend
        rate = cost / basis
        if rate <= 0:
            continue
        p = rate if p is None else (ss * p + (es - ss) * rate) / es
    return p


def blended_price_at(vehicle_id: int, ts: str) -> Optional[float]:
    """Blended €/kWh of the battery (WAC, #53) for `vehicle_id` at instant `ts` — the price in
    effect for a trip starting then, set by every PRICED charge that ended at/before `ts`. None until
    the first priced charge (early trips stay uncosted, as today). Recomputed from history each call
    (no stored state) → self-corrects the moment a charge's cost is assigned/edited."""
    db = _get()
    rows = db.execute(
        "SELECT start_soc, end_soc, cost, ac_energy_kwh, location_type, energy_added_kwh "
        "FROM charges WHERE vehicle_id = ? AND ended_at IS NOT NULL AND ended_at <= ? "
        "  AND cost IS NOT NULL AND energy_added_kwh > 0 ORDER BY ended_at",
        (vehicle_id, ts),
    ).fetchall()
    return _wac_blend([dict(r) for r in rows])


def get_trip_detail(trip_id: int) -> Optional[dict]:
    db = _get()
    row = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not row:
        return None
    # A merged child resolves to (and shows) its parent group.
    parent_id = row["merged_into_id"] or row["id"]
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (parent_id,)).fetchone()
    children = _children_by_parent(db).get(parent_id, [])
    seg_ids = _segment_ids(db, parent_id)
    ph = ",".join("?" * len(seg_ids))
    positions = db.execute(
        "SELECT recorded_at, latitude, longitude, speed_kmh, soc FROM trip_positions "
        f"WHERE trip_id IN ({ph}) ORDER BY recorded_at, id",
        seg_ids,
    ).fetchall()
    trip_d = _trip_group_stats(dict(trip), children)
    if trip_d.get("is_merged"):
        elapsed = _gap_minutes(trip_d.get("started_at"), trip_d.get("ended_at"))
        trip_d["stop_min"] = (round(max(elapsed - (trip_d.get("duration_min") or 0), 0))
                              if elapsed is not None else None)
    trip_d["started_at"] = _local_iso(trip_d.get("started_at"))
    trip_d["ended_at"] = _local_iso(trip_d.get("ended_at"))

    # Speed stats derived from the GPS track (speed_kmh per point).
    speeds = [p["speed_kmh"] for p in positions if p["speed_kmh"] is not None]
    trip_d["max_speed_kmh"] = round(max(speeds)) if speeds else None
    # Average over moving points only (>1 km/h) so long idle stretches don't skew it.
    moving = [s for s in speeds if s > 1]
    trip_d["avg_speed_kmh"] = round(sum(moving) / len(moving)) if moving else None

    # ── #18: total energy consumed + trip cost ──────────────────────────────────
    # Energy consumed = efficiency × distance / 100 (consistent with the stored efficiency).
    eff = trip_d.get("efficiency_kwh_100km")
    dist = trip_d.get("distance_km") or 0
    trip_d["energy_kwh"] = round(eff * dist / 100, 2) if (eff and dist) else None
    # Cost = trip energy × the battery's BLENDED €/kWh at the trip's start (weighted-average-cost,
    # GitHub #53). Replaces the old "rate of the single last charge", which over-billed every trip
    # after an expensive top-up (a small public charge made all the cheaper home energy bill at the
    # premium rate). The blend mixes every PRICED charge by the energy it added (blended_price_at /
    # _wac_blend); unconfirmed charges don't move it (Mate's "no cost until confirmed, HOME excluded").
    # Stores the number only — the `money` filter applies the currency. Final trip cost → 2 decimals.
    trip_d["cost"] = None
    trip_d["cost_per_kwh"] = None
    if trip_d["energy_kwh"]:
        rate = blended_price_at(trip["vehicle_id"], trip["started_at"])
        if rate and rate > 0:
            trip_d["cost_per_kwh"] = round(rate, 4)
            trip_d["cost"] = round(trip_d["energy_kwh"] * rate, 2)

    return {
        **trip_d,
        "positions": [dict(p) for p in positions],
    }


def get_trip_route(trip_id: int, max_points: int = 80) -> list[dict]:
    """Lat/lon track for a single trip, downsampled to at most ``max_points``
    points — used to draw the lightweight route thumbnail in the trips list."""
    db = _get()
    ids = _segment_ids(db, trip_id)
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        "SELECT latitude, longitude FROM trip_positions "
        f"WHERE trip_id IN ({ph}) AND latitude IS NOT NULL AND longitude IS NOT NULL "
        "ORDER BY recorded_at, id",
        ids,
    ).fetchall()
    pts = [dict(r) for r in rows]
    if len(pts) <= max_points:
        return pts
    step = len(pts) / max_points
    sampled = [pts[int(i * step)] for i in range(max_points)]
    sampled[-1] = pts[-1]  # always keep the real end point
    return sampled


def get_charges(limit: int = 50) -> list[dict]:
    db = _get()
    rows = db.execute(
        "SELECT * FROM charges WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["started_at"] = _local_iso(d.get("started_at"))
        d["ended_at"] = _local_iso(d.get("ended_at"))
        out.append(d)
    return out


def get_charge_power_curve(charge_id: int) -> dict:
    """Per-sample charging power for one session, for the expandable power chart.
    Power = |pack_voltage(1177) x pack_current(1178)| / 1000 — the same value as the
    HA `sensor.leapmotor_charging_power`. NOT rounded to 1 decimal (that flattens the
    curve); kept at 3 decimals so the real variation shows. Samples come from the
    general `positions` log (may be pruned over time → empty for very old sessions)."""
    db = _get()
    ch = db.execute("SELECT started_at, ended_at FROM charges WHERE id = ?", (charge_id,)).fetchone()
    if not ch:
        return {"labels": [], "power": [], "soc": []}
    start, end = ch["started_at"], ch["ended_at"]
    if end:
        # Cap the upper bound at the next charge's start so an orphan/overlapping charge
        # (whose ended_at bled past a later charge — see close_orphan_charges) cannot absorb
        # the next charge's power samples into its curve. That leak would inflate BOTH the
        # AC-vs-DC wallbox comparison AND the HOME cost (which bills the AC energy derived from
        # this curve) — GitHub #24. Mirrors _charge_active_window / compute_cost. For a normal
        # charge the next charge starts after ended_at → no cap, identical behaviour.
        lo, hi, excl = _power_window_bounds(db, start, end)
        rows = db.execute(
            "SELECT recorded_at, charge_voltage_v, charge_current_a, soc FROM positions "
            "WHERE charging = 1 AND recorded_at >= ? AND recorded_at " + ("<" if excl else "<=")
            + " ? ORDER BY recorded_at",
            (lo, hi),
        ).fetchall()
    else:  # charge still in progress — open upper bound
        rows = db.execute(
            "SELECT recorded_at, charge_voltage_v, charge_current_a, soc FROM positions "
            "WHERE charging = 1 AND recorded_at >= ? ORDER BY recorded_at",
            (start,),
        ).fetchall()
    labels, power, soc, times = [], [], [], []
    for r in rows:
        v = r["charge_voltage_v"] or 0
        a = r["charge_current_a"] or 0
        labels.append((_local_iso(r["recorded_at"]) or "")[11:16])  # HH:MM local
        power.append(round(abs(v * a) / 1000.0, 3))
        soc.append(r["soc"])
        times.append(r["recorded_at"])  # raw UTC ISO — used to align external (wallbox) history
    return {"labels": labels, "power": power, "soc": soc, "times": times}


def latest_charge_id_with_power() -> int | None:
    """Most recent charge that still has per-sample data (for the Wallbox page chart)."""
    db = _get()
    row = db.execute(
        "SELECT c.id FROM charges c WHERE EXISTS ("
        "  SELECT 1 FROM positions p WHERE p.charging = 1"
        "  AND p.recorded_at >= c.started_at"
        "  AND (c.ended_at IS NULL OR p.recorded_at <= c.ended_at)"
        ") ORDER BY c.started_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def charges_with_power(limit: int = 30) -> list[dict]:
    """Recent HOME charges (= the wallbox) that still have a power curve — raw
    {id, started_at, energy_added_kwh}. Only HOME charges are relevant to the
    wallbox comparison: public/away charges (and unconfirmed NULL ones) are excluded,
    which also avoids attributing another car's wallbox session to this car."""
    db = _get()
    rows = db.execute(
        "SELECT c.id, c.started_at, c.energy_added_kwh FROM charges c "
        "WHERE c.location_type = 'HOME' AND EXISTS ("
        "  SELECT 1 FROM positions p WHERE p.charging = 1"
        "  AND p.recorded_at >= c.started_at"
        "  AND (c.ended_at IS NULL OR p.recorded_at <= c.ended_at)"
        ") ORDER BY c.started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_home_charge(charge_id: int) -> bool:
    """True only when the charge is tagged HOME (= the wallbox)."""
    db = _get()
    row = db.execute("SELECT location_type FROM charges WHERE id = ?", (charge_id,)).fetchone()
    return bool(row) and row["location_type"] == "HOME"


def unconfirmed_charges_count() -> int:
    """How many FINISHED charges still have no type set (location_type NULL) → need
    confirming. In-progress charges (ended_at NULL) are excluded: they can't be
    confirmed until they end, otherwise the banner would never clear while charging."""
    db = _get()
    row = db.execute(
        "SELECT COUNT(*) n FROM charges WHERE location_type IS NULL AND ended_at IS NOT NULL"
    ).fetchone()
    return row["n"] if row else 0


def latest_home_charge_cost():
    """Cost of the most recent home charge (= the wallbox) — from Mate's own charge
    records, so the Wallbox page reuses it instead of a separate HA cost sensor."""
    db = _get()
    row = db.execute(
        "SELECT cost FROM charges WHERE location_type = 'HOME' AND cost IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row["cost"] if row else None


def get_stats_grouped() -> list[dict]:
    """Trip stats nested as year → month → day (aggregated, no individual trips)."""
    from collections import OrderedDict
    db = _get()
    rows = db.execute("""
        SELECT
            strftime('%Y', started_at)    AS year,
            strftime('%Y-%m', started_at) AS month_key,
            date(started_at)              AS day_key,
            COUNT(*)                      AS trip_count,
            ROUND(SUM(distance_km), 2)    AS total_km,
            ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km, 0) / 100), 2) AS total_kwh,
            ROUND(
                SUM(distance_km * COALESCE(efficiency_kwh_100km, 0) / 100) /
                NULLIF(SUM(CASE WHEN efficiency_kwh_100km IS NOT NULL
                               THEN distance_km END), 0) * 100, 1
            ) AS avg_efficiency,
            ROUND(SUM(regen_kwh), 2) AS total_regen_kwh
        FROM trips
        WHERE ended_at IS NOT NULL
        GROUP BY year, month_key, day_key
        ORDER BY started_at DESC
    """).fetchall()

    lang = get_language()
    years: dict = OrderedDict()
    for r in rows:
        d = dict(r)
        yr, mo_key, day_key = d["year"], d["month_key"], d["day_key"]

        # Localize labels in Python (SQLite %B/%b not supported; strftime is English-only)
        try:
            mo_dt  = datetime.strptime(mo_key, "%Y-%m")
            mo_label = i18n.fmt_month_year(lang, mo_dt)
            day_dt   = datetime.strptime(day_key, "%Y-%m-%d")
            d["day_label"] = i18n.fmt_day_month_year(lang, day_dt)
        except Exception:
            mo_label = mo_key
            d["day_label"] = day_key

        if yr not in years:
            years[yr] = {"label": yr, "trip_count": 0, "total_km": 0.0,
                         "total_kwh": 0.0, "total_regen_kwh": 0.0,
                         "_ws": 0.0, "_wd": 0.0,
                         "avg_efficiency": None, "months": OrderedDict()}
        if mo_key not in years[yr]["months"]:
            years[yr]["months"][mo_key] = {"label": mo_label, "trip_count": 0,
                                           "total_km": 0.0, "total_kwh": 0.0,
                                           "total_regen_kwh": 0.0,
                                           "_ws": 0.0, "_wd": 0.0,
                                           "avg_efficiency": None, "days": []}

        years[yr]["months"][mo_key]["days"].append(d)

        km  = d.get("total_km") or 0
        eff = d.get("avg_efficiency")
        for node in (years[yr], years[yr]["months"][mo_key]):
            node["trip_count"]      += d["trip_count"]
            node["total_km"]         = round(node["total_km"] + km, 2)
            node["total_kwh"]        = round(node["total_kwh"] + (d.get("total_kwh") or 0), 2)
            node["total_regen_kwh"]  = round(node["total_regen_kwh"] + (d.get("total_regen_kwh") or 0), 2)
            if eff and km > 0:
                node["_ws"] += km * eff
                node["_wd"] += km

    for yr_node in years.values():
        if yr_node["_wd"] > 0:
            yr_node["avg_efficiency"] = round(yr_node["_ws"] / yr_node["_wd"], 1)
        for mo_node in yr_node["months"].values():
            if mo_node["_wd"] > 0:
                mo_node["avg_efficiency"] = round(mo_node["_ws"] / mo_node["_wd"], 1)
            mo_node["trips"] = []

    # Attach individual trips (chronological ASC) to each month for per-trip charts
    db2 = _get()
    trip_rows = db2.execute(
        """SELECT id, started_at, distance_km, efficiency_kwh_100km, regen_kwh
           FROM trips WHERE ended_at IS NOT NULL ORDER BY started_at ASC"""
    ).fetchall()
    for r in trip_rows:
        t = dict(r)
        if not t.get("started_at"):
            continue
        dt = _local_dt(t["started_at"])
        if dt is None:
            continue
        yr, mo_key = dt.strftime("%Y"), dt.strftime("%Y-%m")
        t["label"] = dt.strftime("%d/%m %H:%M")
        if yr in years and mo_key in years[yr]["months"]:
            years[yr]["months"][mo_key]["trips"].append(t)

    return list(years.values())


def get_monthly_stats() -> list[dict]:
    db = _get()
    rows = db.execute(
        """SELECT
               strftime('%Y-%m', started_at) AS month,
               COUNT(*)                       AS trip_count,
               ROUND(SUM(distance_km), 2)     AS total_km,
               ROUND(SUM(CASE WHEN efficiency_kwh_100km IS NOT NULL
                              THEN distance_km END), 2) AS km_with_eff,
               ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km,0) / 100), 2) AS total_kwh,
               ROUND(AVG(efficiency_kwh_100km), 1) AS avg_efficiency
           FROM trips
           WHERE ended_at IS NOT NULL
           GROUP BY month
           ORDER BY month DESC
           LIMIT 12""",
    ).fetchall()
    return [dict(r) for r in rows]


def _iso_to_utc(x):
    """Normalize any ISO timestamp to a UTC (+00:00) string so it compares correctly against
    positions.recorded_at (stored in UTC). get_charges() hands us LOCAL-offset timestamps, and a raw
    string compare of differently-offset ISO values is wrong — so always convert to UTC first."""
    if not x:
        return x
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(x)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat()
    except Exception:
        return x


def _charge_active_window(db, started_at, ended_at):
    """First & last sample with REAL charging power (positions.charging=1, which is set only when power
    flows — NOT on plug-in) inside the session window. Returns (start_utc_iso, end_utc_iso), or
    (None, None) when there are no power samples (e.g. pruned/old charges). Bounds are normalized to UTC
    because positions.recorded_at is UTC while the charge timestamps may arrive localized."""
    if not started_at:
        return None, None
    # Cap at the next charge's start so an orphan/overlapping charge (whose ended_at can
    # bleed past a later charge — see the poller's close_orphan_charges) cannot inherit the
    # next charge's last power sample as its own window end.
    lo, hi, excl = _power_window_bounds(db, started_at, ended_at)
    row = db.execute(
        "SELECT MIN(recorded_at) AS s, MAX(recorded_at) AS e FROM positions "
        "WHERE charging = 1 AND recorded_at >= ? AND recorded_at " + ("<" if excl else "<=") + " ?",
        (lo, hi),
    ).fetchone()
    return (row["s"], row["e"]) if (row and row["s"]) else (None, None)


def _charge_window_display(db, raw_start, raw_end) -> dict:
    """For the charges list: surface the REAL charging window (first→last power) only when it differs
    from the plug-in→unplug session window by more than a threshold — i.e. a delayed/scheduled charge
    or a long idle tail. For a normal charge the two coincide → {differs: False} (no extra clutter).
    Returns {differs: False} or {differs: True, real_start, real_end} (HH:MM, local)."""
    rs, re = _charge_active_window(db, raw_start, raw_end)
    if not rs:
        return {"differs": False}
    import datetime

    def _p(x):
        try:
            return datetime.datetime.fromisoformat(x)
        except Exception:
            return None

    s0, e0, rs0, re0 = _p(raw_start), _p(raw_end), _p(rs), _p(re)
    THRESH = 300  # seconds — below this the windows are "the same" (just poll granularity)
    differs = bool((s0 and rs0 and (rs0 - s0).total_seconds() > THRESH)
                   or (e0 and re0 and (e0 - re0).total_seconds() > THRESH))
    if not differs:
        return {"differs": False}
    return {"differs": True,
            "real_start": (_local_iso(rs) or "")[11:16],
            "real_end": (_local_iso(re) or "")[11:16]}


def _billed_kwh(c) -> float:
    """The energy figure SHOWN (and billed) for a charge: the wallbox-measured AC kWh for
    HOME charges that have a wallbox reading (what you actually pay for, conversion losses
    included), else the battery DC (SoC) energy. Single source of truth so the per-charge
    card, the period totals and get_charge_stats all agree. Mirrors the SQL CASE in
    get_charge_stats and the card's `show_wb` condition (charges.html)."""
    ac = c.get("ac_energy_kwh")
    if c.get("location_type") == "HOME" and ac and ac > 0:
        return ac
    return c.get("energy_added_kwh") or 0


def get_charges_grouped() -> list[dict]:
    """Return charges nested as year → month → day."""
    charges = get_charges()
    from collections import OrderedDict
    db = _get()

    def _node(label):
        return {"label": label, "count": 0, "kwh": 0.0, "cost": 0.0, "has_cost": False, "months": OrderedDict()}

    def _day_node(label):
        return {"label": label, "count": 0, "kwh": 0.0, "cost": 0.0, "has_cost": False, "charges": []}

    lang = get_language()
    years: dict = OrderedDict()
    for c in charges:
        if not c.get("started_at"):
            continue
        dt = _local_dt(c["started_at"])
        if dt is None:
            continue
        # Real charging window (first→last power) vs the plug-in→unplug session — compute on the RAW
        # UTC timestamps BEFORE we localize them below.
        c["active_window"] = _charge_window_display(db, c.get("started_at"), c.get("ended_at"))
        c["started_at"] = dt.isoformat()
        c["ended_at"] = _local_iso(c.get("ended_at"))

        yr  = dt.strftime("%Y")
        mo  = i18n.fmt_month_year(lang, dt)
        day = i18n.fmt_day_month_year(lang, dt)

        years.setdefault(yr, _node(yr))
        years[yr]["months"].setdefault(mo, {**_node(mo), "days": OrderedDict()})
        years[yr]["months"][mo]["days"].setdefault(day, _day_node(day))

        years[yr]["months"][mo]["days"][day]["charges"].append(c)

        kwh  = _billed_kwh(c)   # wallbox AC for HOME (billed); DC otherwise — matches the card
        cost = c.get("cost") or 0
        for node in [years[yr], years[yr]["months"][mo], years[yr]["months"][mo]["days"][day]]:
            node["kwh"]   = round(node["kwh"] + kwh, 2)
            node["count"] += 1
            if c.get("cost") is not None:
                node["cost"]     = round(node["cost"] + cost, 2)
                node["has_cost"] = True

    return list(years.values())


def get_stats_summary() -> dict:
    db = _get()
    trips = db.execute(
        """SELECT
               COUNT(*)                                                       AS trip_count,
               ROUND(SUM(distance_km), 2)                                    AS total_km,
               ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km,0)/100), 2) AS total_kwh_used,
               ROUND(SUM(duration_min), 0)                                   AS total_drive_min,
               -- distance-weighted = total energy / total distance (#42): a simple AVG
               -- over-weights short trips and disagreed with both the Trips-page header
               -- and this page's own "energy used ÷ distance". Matches get_trips_summary.
               ROUND(SUM(distance_km * efficiency_kwh_100km) /
                     NULLIF(SUM(CASE WHEN efficiency_kwh_100km IS NOT NULL
                                     THEN distance_km END), 0), 1)           AS avg_efficiency,
               ROUND(MIN(CASE WHEN efficiency_kwh_100km > 0 THEN efficiency_kwh_100km END), 1) AS best_efficiency,
               ROUND(SUM(regen_kwh), 2)                                      AS total_regen_kwh,
               ROUND(AVG(regen_kwh), 2)                                      AS avg_regen_kwh
           FROM trips WHERE ended_at IS NOT NULL"""
    ).fetchone()
    charges = db.execute(
        """SELECT
               COUNT(*)                         AS charge_count,
               ROUND(SUM(energy_added_kwh), 2)  AS total_kwh_charged,
               ROUND(SUM(cost), 2)              AS total_cost
           FROM charges WHERE ended_at IS NOT NULL"""
    ).fetchone()
    t = dict(trips) if trips else {}
    c = dict(charges) if charges else {}
    total_kwh = t.get("total_kwh_used") or 0
    total_regen = t.get("total_regen_kwh") or 0
    t["regen_pct"] = round(total_regen / total_kwh * 100, 1) if total_kwh > 0 else None
    return {**t, **c}


def get_charge_stats() -> dict:
    db = _get()
    row = db.execute(
        """SELECT
               COUNT(*)                            AS session_count,
               -- billed energy: wallbox AC for HOME w/ a reading, else battery DC (mirrors _billed_kwh)
               ROUND(SUM(CASE WHEN location_type='HOME' AND ac_energy_kwh IS NOT NULL AND ac_energy_kwh > 0
                              THEN ac_energy_kwh ELSE energy_added_kwh END), 2)  AS total_kwh,
               ROUND(AVG(duration_min / 60.0), 1) AS avg_duration_h,
               ROUND(SUM(cost), 2)                AS total_cost,
               ROUND(AVG(end_soc - start_soc), 1) AS avg_soc_delta,
               ROUND(MAX(max_power_kw), 2)        AS peak_power_kw
           FROM charges
           WHERE ended_at IS NOT NULL"""
    ).fetchone()
    return dict(row) if row else {}


def get_ac_dc_stats() -> dict:
    """Count + energy of AC vs DC charge sessions. DC = charge_type 'DC', or (when not
    set) a measured peak power above 11 kW (AC tops out at ~11 kW; DC is faster)."""
    db = _get()
    rows = db.execute(
        "SELECT charge_type, max_power_kw, energy_added_kwh FROM charges WHERE ended_at IS NOT NULL"
    ).fetchall()
    ac = {"count": 0, "kwh": 0.0}
    dc = {"count": 0, "kwh": 0.0}
    for r in rows:
        ct = r["charge_type"]
        is_dc = ct == "DC" or (ct is None and (r["max_power_kw"] or 0) > 11)
        b = dc if is_dc else ac
        b["count"] += 1
        b["kwh"] += r["energy_added_kwh"] or 0
    ac["kwh"] = round(ac["kwh"], 2)
    dc["kwh"] = round(dc["kwh"], 2)
    return {"ac": ac, "dc": dc, "total": ac["count"] + dc["count"]}


# ── Monthly report (driving + charging + cost, one month) ──────────────────────

def _month_shift(month_key: str, delta: int) -> str:
    """'YYYY-MM' shifted by `delta` calendar months (delta may be negative)."""
    y, m = int(month_key[:4]), int(month_key[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _report_bucket() -> dict:
    return {
        "trip_count": 0, "total_km": 0.0, "total_kwh_used": 0.0,
        "regen_kwh": 0.0, "drive_min": 0.0,
        "_eff_wsum": 0.0, "_eff_wdist": 0.0, "avg_efficiency": None,
        "charge_count": 0, "charge_kwh": 0.0, "charge_cost": 0.0, "has_cost": False,
        "unconfirmed": 0,
        "home":   {"count": 0, "kwh": 0.0, "cost": 0.0},
        "public": {"count": 0, "kwh": 0.0, "cost": 0.0},
        "_days": {},   # day-of-month -> {"km": float, "cost": float}
    }


def _collect_monthly_buckets() -> dict:
    """Bucket every trip and charge into its LOCAL 'YYYY-MM'. One pass, reused for the
    selected month, the previous month (deltas) and the month list (navigation). Trips come
    from get_trips() (merged-aware, same as the Trips page); charges carry the frozen per-row
    cost and the billed-kWh basis (_billed_kwh) so the report's € matches the Charges page."""
    buckets: dict = {}

    for tr in get_trips(limit=1_000_000):
        dt = _local_dt(tr.get("started_at"))
        if dt is None:
            continue
        b = buckets.setdefault(dt.strftime("%Y-%m"), _report_bucket())
        km  = tr.get("distance_km") or 0
        eff = tr.get("efficiency_kwh_100km")
        b["trip_count"]     += 1
        b["total_km"]       += km
        b["total_kwh_used"] += km * (eff or 0) / 100.0
        b["regen_kwh"]      += tr.get("regen_kwh") or 0
        b["drive_min"]      += tr.get("duration_min") or 0
        if eff and km > 0:
            b["_eff_wsum"]  += km * eff
            b["_eff_wdist"] += km
        b["_days"].setdefault(dt.day, {"km": 0.0, "cost": 0.0})["km"] += km

    for c in get_charges(limit=1_000_000):
        dt = _local_dt(c.get("started_at"))
        if dt is None:
            continue
        b = buckets.setdefault(dt.strftime("%Y-%m"), _report_bucket())
        kwh  = _billed_kwh(c)
        cost = c.get("cost")
        lt   = c.get("location_type")
        b["charge_count"] += 1
        b["charge_kwh"]   += kwh
        if cost is not None:
            b["charge_cost"] += cost
            b["has_cost"]     = True
        grp = b["home"] if lt == "HOME" else (b["public"] if lt else None)
        if grp is not None:
            grp["count"] += 1
            grp["kwh"]   += kwh
            if cost is not None:
                grp["cost"] += cost
        else:
            b["unconfirmed"] += 1   # untyped charge: counted in totals, left out of the split
        if cost is not None:
            b["_days"].setdefault(dt.day, {"km": 0.0, "cost": 0.0})["cost"] += cost

    for b in buckets.values():
        if b["_eff_wdist"] > 0:
            b["avg_efficiency"] = round(b["_eff_wsum"] / b["_eff_wdist"], 1)
        for k in ("total_km", "total_kwh_used", "regen_kwh", "charge_kwh", "charge_cost"):
            b[k] = round(b[k], 2)
        b["drive_min"] = int(round(b["drive_min"]))
        for g in ("home", "public"):
            b[g]["kwh"]  = round(b[g]["kwh"], 2)
            b[g]["cost"] = round(b[g]["cost"], 2)
    return buckets


def get_monthly_report(month: Optional[str] = None) -> dict:
    """One-month digest combining driving, charging and cost, with deltas vs the previous
    calendar month and the list of months that have data (for the ◀ ▶ / dropdown nav).
    `month` = local 'YYYY-MM'; defaults to the most recent month with any data."""
    import calendar
    buckets = _collect_monthly_buckets()
    if not buckets:
        return {"has_data": False, "month": None, "months": []}

    months_desc = sorted(buckets.keys(), reverse=True)
    if not month or month not in buckets:
        month = months_desc[0]

    lang = get_language()
    def _label(mk):
        return i18n.fmt_month_year(lang, datetime.strptime(mk, "%Y-%m"))

    cur      = buckets[month]
    prev_key = _month_shift(month, -1)
    prev     = buckets.get(prev_key)

    older = [m for m in months_desc if m < month]   # desc → nearest past is first
    newer = [m for m in months_desc if m > month]   # desc → nearest future is last

    def _delta(now, was):
        if not was:                                 # None or 0 → no meaningful %
            return {"diff": round(now, 2), "pct": None}
        return {"diff": round(now - was, 2), "pct": int(round((now - was) / was * 100))}

    deltas = None
    if prev:
        eff_d = None
        if cur["avg_efficiency"] is not None and prev["avg_efficiency"] is not None:
            eff_d = _delta(cur["avg_efficiency"], prev["avg_efficiency"])
        deltas = {
            "km":         _delta(cur["total_km"], prev["total_km"]),
            "kwh_used":   _delta(cur["total_kwh_used"], prev["total_kwh_used"]),
            "cost":       _delta(cur["charge_cost"], prev["charge_cost"]),
            "charge_kwh": _delta(cur["charge_kwh"], prev["charge_kwh"]),
            "efficiency": eff_d,
        }

    avg_price = (round(cur["charge_cost"] / cur["charge_kwh"], 3)
                 if cur["charge_kwh"] > 0 and cur["has_cost"] else None)

    ndays = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    daily = [{"day": d,
              "km":   cur["_days"].get(d, {}).get("km", 0.0),
              "cost": cur["_days"].get(d, {}).get("cost", 0.0)}
             for d in range(1, ndays + 1)]

    return {
        "has_data": True,
        "month": month, "label": _label(month),
        "prev_month": older[0] if older else None,
        "next_month": newer[-1] if newer else None,
        "months": [{"key": m, "label": _label(m)} for m in months_desc],
        "cur": cur, "prev": prev, "prev_label": _label(prev_key) if prev else None,
        "deltas": deltas, "avg_price": avg_price, "daily": daily,
    }


# ── Battery health (SoH) ───────────────────────────────────────────────────────

def get_battery_capacity_kwh() -> float:
    """Configured (nominal) usable battery capacity, set per-model at first run and
    overridable in Settings. Used as the 100%-SoC reference for the health estimate."""
    try:
        return float(get_setting("battery_capacity_kwh", "65.0"))
    except (TypeError, ValueError):
        return 65.0


_SCAN_MAX_KW = 250.0  # implied charge rate above this → spurious-SoC glitch, not a real charge


def scan_missed_charges(threshold: float = 2.0, apply: bool = False) -> list[dict]:
    """Find charges that happened while the car was asleep/offline BEFORE live
    reconstruction existed (or while the poller was down) and were never logged — a
    SoC that ROSE while parked, not covered by any existing charge (GitHub #35, from
    the #29 follow-up). Returns candidate dicts; with apply=True also inserts them as
    reconstructed charges (charge_type 'AC', cost NULL until the user confirms the type,
    exactly like the live reconstruction path).

    Idempotent: an applied candidate's window is then covered by its own charge row, so
    a re-run's overlap check skips it — running it twice creates no duplicates.

    Guards against false positives (which a one-shot silent migration could not afford,
    hence this is preview-then-confirm): parked at both ends (charging=0, speed<=1), the
    odometer UNCHANGED across the whole run (so regen while driving offline can't look
    like a charge), and no overlap with any existing charge window."""
    db = _conn_rw() if apply else _get()
    v = db.execute("SELECT id FROM vehicles LIMIT 1").fetchone()
    if not v:
        return []
    vehicle_id = v["id"]
    rows = db.execute(
        "SELECT recorded_at, soc, charging, speed_kmh, odometer_km, latitude, longitude "
        "FROM positions WHERE vehicle_id=? AND soc IS NOT NULL ORDER BY recorded_at, id",
        (vehicle_id,)).fetchall()
    charges = db.execute(
        "SELECT started_at, ended_at FROM charges WHERE vehicle_id=?", (vehicle_id,)).fetchall()
    cap = get_battery_capacity_kwh()

    def _parked(r):
        return (r["charging"] or 0) == 0 and (r["speed_kmh"] or 0) <= 1

    def _odo_same(a, b):
        oa, ob = a["odometer_km"], b["odometer_km"]
        return oa is None or ob is None or abs(ob - oa) < 0.5

    def _overlaps(start, end):
        for c in charges:
            cs, ce = c["started_at"], (c["ended_at"] or "9999")   # NULL end = open-ended
            if start <= ce and cs <= end:                          # inclusive interval overlap
                return True
        return False

    candidates, i, n = [], 0, len(rows)
    while i < n - 1:
        a, b = rows[i], rows[i + 1]
        if not (b["soc"] - a["soc"] > 0 and _parked(a) and _parked(b) and _odo_same(a, b)):
            i += 1
            continue
        # Extend the run while SoC keeps rising, parked, and the odometer never moves —
        # so one charge seen across several stale polls becomes ONE candidate, not many.
        run_start, run_end, j = a, b, i + 1
        while j < n - 1:
            c, d = rows[j], rows[j + 1]
            if d["soc"] - c["soc"] > 0 and _parked(c) and _parked(d) and _odo_same(run_start, d):
                run_end, j = d, j + 1
            else:
                break
        rise = run_end["soc"] - run_start["soc"]
        if rise >= threshold and run_start["soc"] >= 1.0 and not _overlaps(run_start["recorded_at"], run_end["recorded_at"]):
            try:
                dur = round((datetime.fromisoformat(run_end["recorded_at"])
                             - datetime.fromisoformat(run_start["recorded_at"])).total_seconds() / 60, 1)
            except (TypeError, ValueError):
                dur = None
            # Plausibility: a spurious SoC=0/low reading makes a "charge" of impossible power (a full
            # pack in seconds). Skip runs whose implied rate exceeds any real charger; keep when the
            # duration is unknown (start_soc>=1 already filters the zero-start glitch).
            implied_kw = (rise / 100.0 * cap) / (dur / 60.0) if dur and dur > 0 else None
            if implied_kw is not None and implied_kw > _SCAN_MAX_KW:
                i = j + 1
                continue
            candidates.append({
                "started_at": run_start["recorded_at"], "ended_at": run_end["recorded_at"],
                "start_soc": run_start["soc"], "end_soc": run_end["soc"],
                "energy_kwh": round(max(rise / 100.0 * cap, 0), 3), "duration_min": dur,
                "latitude": run_end["latitude"], "longitude": run_end["longitude"],
            })
        i = j + 1

    if apply and candidates:
        for c in candidates:
            db.execute(
                """INSERT INTO charges
                   (vehicle_id, started_at, ended_at, start_soc, end_soc, energy_added_kwh,
                    duration_min, latitude, longitude, charge_type, reconstructed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (vehicle_id, c["started_at"], c["ended_at"], c["start_soc"], c["end_soc"],
                 c["energy_kwh"], c["duration_min"], c["latitude"], c["longitude"], "AC"))
        db.commit()
    return candidates


def _integrate_charge_energy_kwh(db, start: str, end: str | None) -> float:
    """Real DC energy delivered into the pack during a charge = ∫|V·I|dt over the
    logged samples (trapezoidal). V/I come from signals 1177/1178 in `positions`, the
    same source as the power-curve chart and the Wallbox DC comparison. This is a
    MEASURED energy, independent of SoC — so dividing it by the SoC delta gives an
    estimate of usable pack capacity that actually tracks battery ageing (unlike the
    stored energy_added_kwh, which is SoC × nominal capacity and would be circular)."""
    if end:
        # Cap at the next charge's start (same leak guard as get_charge_power_curve / compute_cost)
        # so an overlapping orphan charge can't inflate the integrated DC energy / SoH estimate.
        lo, hi, excl = _power_window_bounds(db, start, end)
        rows = db.execute(
            "SELECT recorded_at, charge_voltage_v, charge_current_a FROM positions "
            "WHERE charging = 1 AND recorded_at >= ? AND recorded_at " + ("<" if excl else "<=")
            + " ? ORDER BY recorded_at",
            (lo, hi),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT recorded_at, charge_voltage_v, charge_current_a FROM positions "
            "WHERE charging = 1 AND recorded_at >= ? ORDER BY recorded_at",
            (start,),
        ).fetchall()
    energy = 0.0
    prev_t = None
    prev_p = 0.0
    for r in rows:
        try:
            t = datetime.fromisoformat(str(r["recorded_at"]).replace(" ", "T").rstrip("Z"))
        except Exception:
            continue
        p = abs((r["charge_voltage_v"] or 0) * (r["charge_current_a"] or 0)) / 1000.0
        if prev_t is not None:
            dt_h = (t - prev_t).total_seconds() / 3600.0
            # Guard against gaps (deep-sleep / pruning): ignore intervals over 15 min.
            if 0 < dt_h <= 0.25:
                energy += (p + prev_p) / 2.0 * dt_h
        prev_t, prev_p = t, p
    return energy


_AC_CHARGE_TYPES = ('AC', 'HOME', 'FREE')   # types where DC fast-rate is impossible


def _charge_has_soc_jump(db, start: str, end: str | None,
                         max_rate_per_min: float = 0.8) -> bool:
    """True if any two consecutive charging samples in the session show a SoC rise rate
    faster than max_rate_per_min %/min — a BMS recalibration snap, not real energy.
    At AC rates (≤ 22 kW on 67 kWh), the physical max is ~0.55%/min; a threshold of 0.8
    leaves margin for fast 3-phase AC while still catching BMS jumps (e.g. +2.5%/min).
    Only call this for AC charge types — DC fast-charging can legitimately reach 3-4%/min."""
    clause = "recorded_at >= ? AND recorded_at <= ?" if end else "recorded_at >= ?"
    params = (start, end) if end else (start,)
    rows = db.execute(
        f"SELECT recorded_at, soc FROM positions WHERE {clause} AND charging = 1 "
        "AND soc IS NOT NULL ORDER BY recorded_at",
        params,
    ).fetchall()
    prev_soc, prev_t = None, None
    for r in rows:
        soc = r["soc"]
        try:
            t = datetime.fromisoformat(str(r["recorded_at"]).replace(" ", "T").rstrip("Z"))
        except Exception:
            prev_soc, prev_t = soc, None
            continue
        if prev_soc is not None and prev_t is not None:
            dt_min = (t - prev_t).total_seconds() / 60.0
            if 0 < dt_min <= 15.0 and (soc - prev_soc) / dt_min > max_rate_per_min:
                return True
        prev_soc, prev_t = soc, t
    return False


def _charge_has_active_use(db, start: str, end: str | None) -> bool:
    """True if any position sample during the charge window had cabin HVAC running
    (climate_cooling=1 or climate_heating=1 — not just climate_on, which also fires during
    battery thermal management and is too broad). A running cabin compressor/heater is a
    reliable proxy for 'user was in the car consuming power', which distorts the energy/SoC
    ratio used for the SoH estimate."""
    clause = "recorded_at >= ? AND recorded_at <= ?" if end else "recorded_at >= ?"
    params = (start, end) if end else (start,)
    row = db.execute(
        f"SELECT 1 FROM positions WHERE {clause} "
        "AND (climate_cooling = 1 OR climate_heating = 1) LIMIT 1",
        params,
    ).fetchone()
    return row is not None


def _charge_temp_odo(db, start: str, end: str | None):
    """Coldest battery temperature (°C) and the odometer (km) seen WHILE CHARGING in a session,
    from the positions log. The min temp is the conservative basis for the cold-charge gate; the
    odometer gives the per-distance (cycle-ageing) axis of the SoH trend."""
    if end:
        rows = db.execute(
            "SELECT battery_min_temp, odometer_km FROM positions WHERE charging = 1 "
            "AND recorded_at >= ? AND recorded_at <= ? ORDER BY recorded_at", (start, end)).fetchall()
    else:
        rows = db.execute(
            "SELECT battery_min_temp, odometer_km FROM positions WHERE charging = 1 "
            "AND recorded_at >= ? ORDER BY recorded_at", (start,)).fetchall()
    temps = [r["battery_min_temp"] for r in rows if r["battery_min_temp"] is not None]
    odos = [r["odometer_km"] for r in rows if r["odometer_km"] is not None]
    return (min(temps) if temps else None), (max(odos) if odos else None)


def get_battery_health(min_soc_delta: float = 12.0, temp_min_c: float | None = None) -> dict:
    """Estimate usable battery capacity / state-of-health over time from charge sessions. For
    each charge with a meaningful SoC rise we integrate the measured DC energy and divide by the
    SoC delta → estimated full-pack capacity.

    Two LFP-specific refinements keep the trend honest:
    - **Cold charges are shown but excluded** from the headline/trend. A cold LFP pack delivers
      less and its BMS SoC drifts, so a winter session reads low — that's temperature, not ageing.
      Charges whose min battery temp is below `temp_min_c` (Settings `soh_temp_min_c`, default 15°C)
      get `excluded: True` and don't feed the figure, but stay in `points` for the chart.
    - **Charges ending near 100% weigh most** in the headline: the BMS recalibrates SoC near full,
      so their SoC delta — and therefore the estimate — is the most trustworthy.

    Single sessions are noisy, so the headline is a weighted mean over the most recent valid ones.
    Charges with no stored telemetry (pruned) are skipped entirely."""
    db = _get()
    # SoH is measured-vs-as-new, so the denominator is the ORIGINAL spec capacity, not
    # the energy-calc capacity the user may have overridden — otherwise adopting a
    # measured (already-aged) value would reset SoH to ~100% and hide the ageing.
    # battery_capacity_nominal_kwh is snapshotted the first time the user overrides.
    try:
        nominal = float(get_setting("battery_capacity_nominal_kwh", "") or get_battery_capacity_kwh())
    except (TypeError, ValueError):
        nominal = get_battery_capacity_kwh()
    if temp_min_c is None:
        try:
            temp_min_c = float(get_setting("soh_temp_min_c", "15") or 15)
        except (TypeError, ValueError):
            temp_min_c = 15.0
    rows = db.execute(
        "SELECT id, started_at, ended_at, start_soc, end_soc, charge_type "
        "FROM charges WHERE ended_at IS NOT NULL AND start_soc IS NOT NULL "
        "AND end_soc IS NOT NULL ORDER BY started_at",
    ).fetchall()
    points = []
    for r in rows:
        delta = (r["end_soc"] or 0) - (r["start_soc"] or 0)
        if delta < min_soc_delta:                      # tiny top-ups → huge relative error
            continue
        energy = _integrate_charge_energy_kwh(db, r["started_at"], r["ended_at"])
        if energy <= 0.1:                              # no usable telemetry (pruned / AC-only meter)
            continue
        est = energy / (delta / 100.0)
        # Drop physically implausible estimates (sampling gaps, bad V/I spikes).
        if not (nominal * 0.5 <= est <= nominal * 1.15):
            continue
        temp, odo = _charge_temp_odo(db, r["started_at"], r["ended_at"])
        cold = temp is not None and temp < temp_min_c
        soc_jump = (not cold and r["charge_type"] in _AC_CHARGE_TYPES
                    and _charge_has_soc_jump(db, r["started_at"], r["ended_at"]))
        active_use = (not cold and not soc_jump
                      and _charge_has_active_use(db, r["started_at"], r["ended_at"]))
        excluded = cold or soc_jump or active_use
        exclude_reason = ("cold" if cold else ("soc_jump" if soc_jump else "active_use")) if excluded else None
        dt = _local_dt(r["started_at"])
        points.append({
            "charge_id": r["id"],
            "date": dt.strftime("%Y-%m-%d") if dt else (r["started_at"] or "")[:10],
            "ts": dt.isoformat() if dt else r["started_at"],
            "capacity_kwh": round(est, 1),
            "soh_pct": round(est / nominal * 100, 1) if nominal else None,
            "soc_delta": round(delta, 1),
            "end_soc": round(r["end_soc"], 1) if r["end_soc"] is not None else None,
            "energy_kwh": round(energy, 2),
            "temp_c": round(temp, 1) if temp is not None else None,
            "odometer_km": round(odo) if odo is not None else None,
            "charge_type": r["charge_type"],
            "excluded": excluded,
            "exclude_reason": exclude_reason,
        })
    valid = [p for p in points if not p["excluded"]]

    # Weight a session by how close it ended to a full (BMS-recalibrated) 100% — that's where the
    # LFP SoC is trustworthy, so its SoC delta (and the estimate) carries the least error.
    def _w(p):
        es = p.get("end_soc")
        return 1.0 if es is None else max(0.25, min(1.0, (es - 50.0) / 50.0))

    tail = valid[-5:]                                  # weighted mean of the recent valid estimates
    if tail:
        wsum = sum(_w(p) for p in tail)
        latest_cap = round(sum(p["capacity_kwh"] * _w(p) for p in tail) / wsum, 1)
        latest_soh = round(latest_cap / nominal * 100, 1) if nominal else None
    else:
        latest_cap = latest_soh = None
    return {
        "nominal_kwh": round(nominal, 1),
        "points": points,
        "sample_count": len(valid),
        "excluded_count": len(points) - len(valid),
        "cold_count": sum(1 for p in points if p.get("exclude_reason") == "cold"),
        "active_use_count": sum(1 for p in points if p.get("exclude_reason") == "active_use"),
        "soc_jump_count": sum(1 for p in points if p.get("exclude_reason") == "soc_jump"),
        "temp_min_c": round(temp_min_c, 1),
        "latest_capacity_kwh": latest_cap,
        "latest_soh_pct": latest_soh,
    }


# SoC arrives as preciseSoc (signal 100003) with 0.1% resolution, and a ±0.1% parked BMS
# jitter is real (both up- and down-ticks observed while parked, odometer flat). Worst case
# each window endpoint is one quantum off, so a window's drop carries up to ±0.2% of pure
# measurement error — which the %/day extrapolation multiplies by 24/hours (#41).
SOC_QUANTUM = 0.1
_DROP_ERR = 2 * SOC_QUANTUM
# The intrinsic noise floor: a parked drop below 2 sensor quanta is jitter, not drain. The user's
# `vampire_min_drop_pct` is a DISPLAY threshold layered on top — raising it thins the charted bars,
# but it must never make a car that DOES lose charge look like it has no parked data at all (#63).
# So we always collect windows down to this floor and tag which ones clear the user's threshold,
# letting the page tell "no parked data yet" apart from "data exists, just below your threshold".
_VAMPIRE_NOISE_FLOOR = 0.2


_VAMPIRE_ACTIVE_USE_RATE = 15.0  # %/day above this is active use (A/C, meeting, etc.), not standby


def get_vampire_drain(min_hours: float = 1.0, min_drop_pct: float = 0.2,
                      lookback_days: int = 90, limit: int = 60) -> dict:
    """Vampire drain = SoC lost while the car is PARKED and NOT charging. Scans the per-poll
    `positions` log, groups consecutive parked-idle samples (charging=0, not moving) into windows
    bounded by any charging or driving — driving is detected by speed OR a rise in odometer between
    idle samples, so a drive that happened during a reporting gap can't be mistaken for drain. Each
    kept window reports its SoC drop, a normalised %/day rate, the rate's quantization error band
    (`rate_err`) and whether the rate is trustworthy (`reliable`: a drop of at least 4 quanta AND
    an error band within ±1 %/day — short windows extrapolate a single sensor step into several
    %/day, see #41). Windows shorter than `min_hours` or with a drop below `min_drop_pct` (sensor
    jitter) are not charted, but every park >= `min_hours` — zero-drop ones included — feeds the
    time-weighted `typical_pct_per_day` headline. Pure read over data Mate already records every
    poll — no extra polling, no user input."""
    db = _get()
    # Collect down to the intrinsic noise floor regardless of the user's display threshold, so a
    # raised `min_drop_pct` thins the chart without hiding that drain exists at all (#63).
    floor = min(min_drop_pct, _VAMPIRE_NOISE_FLOOR)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.execute(
        "SELECT recorded_at, soc, charging, speed_kmh, odometer_km FROM positions "
        "WHERE soc IS NOT NULL AND recorded_at >= ? ORDER BY recorded_at",
        (cutoff,),
    ).fetchall()

    windows = []
    agg = {"drop": 0.0, "hours": 0.0}

    def _flush(w, ongoing=False, close=None):
        if not w:
            return
        soc_end, t_end = w["soc_last"], w["t_last"]
        # The park ended at a wake into driving/charging: the first fresh reading reveals the SoC
        # that actually drained DURING deep sleep — while asleep the car stops reporting and the
        # cloud serves a FROZEN SoC, so the parked samples sit flat and a slow loss is invisible
        # until wake (and is otherwise lost if the car is driven right away: the parked window
        # closes at the frozen value and the drop falls in the gap before the trip's start SoC).
        # Close the window at that fresh value + time so the drain is captured — but only when it's
        # a DROP (a rise = BMS recalibration / charge → keep the parked value, never invent drain).
        if close is not None and close["soc"] is not None and close["soc"] < (soc_end or 0):
            soc_end, t_end = close["soc"], close["recorded_at"]
        t0, t1 = _local_dt(w["t0"]), _local_dt(t_end)
        if t0 is None or t1 is None:
            return
        hours = (t1 - t0).total_seconds() / 3600.0
        drop = (w["soc0"] or 0) - (soc_end or 0)
        pct_per_day = drop / hours * 24 if hours else 0
        active_use = pct_per_day > _VAMPIRE_ACTIVE_USE_RATE
        if hours >= min_hours and not active_use:
            # Headline aggregate: every park long enough to measure counts, including zero-drop
            # ones — a "drain happened"-only sample reads high (selection bias). SoC up-ticks
            # while parked are BMS jitter, not charging → clamp to 0.
            # Active-use windows (rate > threshold) are excluded: that's intentional
            # consumption (A/C, screen, user in car), not standby loss.
            agg["hours"] += hours
            agg["drop"] += max(drop, 0.0)
        # Compare the rounded drop: raw float drops sit a hair off the threshold
        # (56.8 − 56.4 = 0.3999…), so identical physical drops would randomly pass/fail.
        drop_r = round(drop, 1)
        if hours >= min_hours and drop_r >= floor - 1e-9:
            err = _DROP_ERR / hours * 24
            windows.append({
                "start": t0.isoformat(), "end": t1.isoformat(),
                "hours": round(hours, 1),
                "soc_start": round(w["soc0"], 1), "soc_end": round(soc_end, 1),
                "drop_pct": drop_r,
                "pct_per_day": round(pct_per_day, 1),
                "rate_err": round(err, 1),
                "reliable": drop_r >= 2 * _DROP_ERR - 1e-9 and err <= 1.0,
                "ongoing": ongoing,
                "active_use": active_use,
                # Clears the user's display threshold → charted as a bar; otherwise it's a real
                # parked window kept only to power the "below your threshold" hint + headline.
                "_charted": drop_r >= min_drop_pct - 1e-9,
            })

    cur = None
    for r in rows:
        idle = (not r["charging"]) and ((r["speed_kmh"] or 0) < 1)
        odo = r["odometer_km"]
        # a rise in odometer since the window's last idle sample → a drive happened (even if its
        # samples were missed) → the park ended there.
        if (cur is not None and odo is not None and cur["odo_last"] is not None
                and odo - cur["odo_last"] > 0.5):
            _flush(cur)
            cur = None
        if not idle:                        # driving / charging now → park ended
            # Close at the wake's fresh SoC only on a DRIVING transition (the odometer-rise guard
            # above already split off any drive that happened in a gap, so a same-odometer drive
            # sample here is a genuine wake-after-park → its SoC is real standby drain). A CHARGING
            # transition is left as-is: the pre-charge gap is ambiguous (could be a drive to the
            # charger), so we never infer drain from it.
            _flush(cur, close=(None if r["charging"] else r))
            cur = None
            continue
        if cur is None:                     # start a new parked window
            cur = {"t0": r["recorded_at"], "soc0": r["soc"],
                   "t_last": r["recorded_at"], "soc_last": r["soc"], "odo_last": odo}
        else:                               # extend the current parked window
            cur["t_last"] = r["recorded_at"]
            cur["soc_last"] = r["soc"]
            if odo is not None:
                cur["odo_last"] = odo
    _flush(cur, ongoing=True)               # the trailing park is still open

    windows = windows[-limit:]
    # Split the kept (>= noise floor) windows into the ones charted at the user's display
    # threshold and the rest. `measurable` = real parked drain that exists regardless of the
    # slider; `below_threshold` powers the "data exists, just below your X% threshold" hint so a
    # raised slider never reads as "no parked data at all" (#63).
    charted = [w for w in windows if w.pop("_charted")]
    measurable = len(windows)
    active_use_count = sum(1 for w in charted if w.get("active_use"))
    # Time-weighted typical (total SoC lost / total parked time): quantization noise cancels
    # across windows instead of every short park voting like a long one, and slow drain below
    # the per-window display threshold still surfaces. Gated on `measurable` (not the charted
    # count) so the headline survives a raised display threshold; None while nothing clears the
    # noise floor, so young installs keep the no-data state.
    typical = round(agg["drop"] / agg["hours"] * 24, 1) if measurable and agg["hours"] else None
    return {"windows": charted, "count": len(charted),
            "measurable_count": measurable, "below_threshold": measurable - len(charted),
            "active_use_count": active_use_count,
            "min_drop_pct": round(min_drop_pct, 1),
            "typical_pct_per_day": typical, "lookback_days": lookback_days}


# ── Global map (all tracks + frequent places) ──────────────────────────────────

def _rows_to_segments(rows, max_points: int) -> list[list[list[float]]]:
    """Group ordered (trip_id, lat, lon) rows into one polyline per trip (never joined across
    trips), then proportionally downsample to ~max_points total while keeping each trip's real
    first/last point. Shared by the global map (get_all_track) and the report's month map."""
    segments: list[list[list[float]]] = []
    cur_id, cur = None, []
    for r in rows:
        if r["trip_id"] != cur_id:
            if len(cur) >= 2:
                segments.append(cur)
            cur, cur_id = [], r["trip_id"]
        cur.append([round(r["latitude"], 5), round(r["longitude"], 5)])
    if len(cur) >= 2:
        segments.append(cur)

    total = sum(len(s) for s in segments)
    if total <= max_points or total == 0:
        return segments
    # Proportional per-trip downsample, keeping each segment's real endpoints.
    step = total / max_points
    out = []
    for s in segments:
        keep = max(2, int(len(s) / step))
        if keep >= len(s):
            out.append(s)
            continue
        st = len(s) / keep
        ds = [s[int(i * st)] for i in range(keep)]
        ds[-1] = s[-1]
        out.append(ds)
    return out


def get_all_track(max_points: int = 12000) -> list[list[list[float]]]:
    """Every trip's GPS track as a list of polylines (one [lat, lon] list per trip),
    so the global map draws the actual driven roads as connected lines instead of
    loose dots. Points are NEVER joined across trips. Downsampled to roughly
    ``max_points`` total while always keeping each trip's first and last point, so the
    lines stay continuous even when zoomed in."""
    db = _get()
    rows = db.execute(
        "SELECT trip_id, latitude, longitude FROM trip_positions "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY trip_id, id"
    ).fetchall()
    return _rows_to_segments(rows, max_points)


def get_month_track(month: str, max_points: int = 8000) -> list[list[list[float]]]:
    """GPS polylines for every trip STARTED in the given local 'YYYY-MM' — the report's month
    map. Same shape/downsampling as get_all_track, scoped to one month's trips (parent and
    merged-child trips alike, so every road driven that month is drawn)."""
    if not month:
        return []
    db = _get()
    ids = []
    for r in db.execute("SELECT id, started_at FROM trips WHERE started_at IS NOT NULL").fetchall():
        dt = _local_dt(r["started_at"])
        if dt is not None and dt.strftime("%Y-%m") == month:
            ids.append(r["id"])
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        "SELECT trip_id, latitude, longitude FROM trip_positions "
        f"WHERE trip_id IN ({ph}) AND latitude IS NOT NULL AND longitude IS NOT NULL "
        "ORDER BY trip_id, id", ids
    ).fetchall()
    return _rows_to_segments(rows, max_points)


def get_frequent_places(min_visits: int = 2, top_n: int = 15) -> list[dict]:
    """Cluster trip start/end points into recurring places (Home, Work, …) by snapping
    coordinates to a ~110 m grid (3 decimals) and counting visits. Returns the busiest
    clusters with an averaged centre and a visit count — no reverse geocoding, so it
    stays offline and cheap."""
    db = _get()
    rows = db.execute(
        "SELECT start_lat, start_lon, end_lat, end_lon FROM trips"
    ).fetchall()
    buckets: dict[tuple, dict] = {}
    for r in rows:
        for lat, lon in ((r["start_lat"], r["start_lon"]), (r["end_lat"], r["end_lon"])):
            if lat is None or lon is None:
                continue
            key = (round(lat, 3), round(lon, 3))
            b = buckets.setdefault(key, {"lat": 0.0, "lon": 0.0, "visits": 0})
            b["lat"] += lat
            b["lon"] += lon
            b["visits"] += 1
    places = [
        {"latitude": round(b["lat"] / b["visits"], 6),
         "longitude": round(b["lon"] / b["visits"], 6),
         "visits": b["visits"]}
        for b in buckets.values() if b["visits"] >= min_visits
    ]
    places.sort(key=lambda p: p["visits"], reverse=True)
    return places[:top_n]
