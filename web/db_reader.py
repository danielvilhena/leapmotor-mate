"""Read-only DB queries for the web layer."""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import os

# Timestamps are stored in UTC (poller uses datetime.now(timezone.utc)). The UI
# must show local time. TZ comes from the environment (Home Assistant passes the
# system timezone to add-ons automatically); standalone Docker sets it in compose.
try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Rome")
except Exception:
    _LOCAL_TZ = timezone.utc


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


CHARGE_TYPES = {
    "HOME": {"label": "Home",       "icon": "🏠", "color": "#22c55e"},
    "AC":   {"label": "AC Public",  "icon": "🔌", "color": "#60a5fa"},
    "FAST": {"label": "Fast DC",    "icon": "⚡", "color": "#fb923c"},
    "HPC":  {"label": "HPC",        "icon": "🚀", "color": "#e879f9"},
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


def set_sunshade_state(open: int) -> None:
    """Persist the sunshade state from the last command (signal 1724 is unreliable for shade)."""
    set_setting("sunshade_last_state", str(open))


def get_language() -> str:
    return get_setting("language", "en")


def get_charge_prices() -> dict:
    db = _get()
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'price_%_kwh'"
    ).fetchall()
    return {r["key"]: float(r["value"]) for r in rows}


def update_charge_type(charge_id: int, location_type: str) -> dict:
    """Set location_type and recalculate cost. Returns updated charge dict."""
    db = _conn_rw()
    prices = get_charge_prices()
    price_key = PRICE_KEYS.get(location_type)
    price = prices.get(price_key, 0.0) if price_key else 0.0

    charge = db.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()
    if not charge:
        return {}

    energy = charge["energy_added_kwh"] or 0
    cost   = round(energy * price, 2) if price else None

    db.execute(
        "UPDATE charges SET location_type=?, cost=? WHERE id=?",
        (location_type, cost, charge_id)
    )
    db.commit()
    return dict(db.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone())


def update_charge_price(key: str, value: float) -> None:
    db = _conn_rw()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    # Recalculate costs for all charges with a location_type
    for ctype, pkey in PRICE_KEYS.items():
        if pkey == key:
            charges = db.execute(
                "SELECT id, energy_added_kwh FROM charges WHERE location_type=?", (ctype,)
            ).fetchall()
            for c in charges:
                cost = round((c["energy_added_kwh"] or 0) * value, 2)
                db.execute("UPDATE charges SET cost=? WHERE id=?", (cost, c["id"]))
    db.commit()


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
    windows_open = int(any(sig(k) != 0 for k in ("1693", "1694", "1695", "1696")))

    # Plug from signal 47 (reliable, stays 0 while driving) — 1149 only as fallback,
    # since it reads 1 spuriously during regen at speed. Matches leapmotor-ha.
    _plug47 = signals.get("47")
    plug_connected = (int(_plug47) == 1) if _plug47 is not None else (sig("1149") in (1, 2))

    db.execute(
        """INSERT INTO positions (
            vehicle_id, recorded_at,
            latitude, longitude, speed_kmh, odometer_km,
            soc, range_km, gear, charging,
            battery_min_temp, climate_target_temp, inside_temp,
            is_locked, climate_on, plug_connected,
            climate_cooling, climate_heating, climate_defrost,
            trunk_open, windows_open, sunshade_open,
            remaining_charge_min, charge_voltage_v, charge_current_a
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    # Sunshade: signal 1724 is the panoramic glass (always non-zero on B10), not the shade.
    # Override with the last command we sent, stored in settings.
    shade_state = get_setting("sunshade_last_state", "")
    if shade_state != "" and "sunshade_open" not in _opt_overrides:
        d["sunshade_open"] = int(shade_state)
    # Charge power: positions stores current/voltage, not a power column. Compute it
    # (|I×V|), only when the charge current is meaningful (>=3A). Signal 49 is NOT a
    # power (it's the left-mirror-heating flag) and must never be used here.
    cur_a = d.get("charge_current_a")
    volt_v = d.get("charge_voltage_v")
    if cur_a is not None and volt_v is not None and abs(cur_a) >= 3.0:
        d["charge_power_kw"] = round(abs(cur_a * volt_v) / 1000.0, 2)
    else:
        d["charge_power_kw"] = 0.0
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
    return d


def get_trips(limit: int = 500) -> list[dict]:
    db = _get()
    rows = db.execute(
        """SELECT * FROM trips
           WHERE ended_at IS NOT NULL
           ORDER BY started_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_trips_grouped() -> list[dict]:
    """Return trips nested as year → month → day for the sidebar tree view."""
    trips = get_trips()
    from collections import OrderedDict

    def _node(label):
        return {"label": label, "km": 0, "count": 0,
                "_eff_wsum": 0.0, "_eff_wdist": 0.0, "avg_eff": None}

    def _add(node, km, eff):
        node["km"]    = round(node["km"] + km, 1)
        node["count"] += 1
        if eff and km > 0:
            node["_eff_wsum"]  += km * eff
            node["_eff_wdist"] += km

    def _finalize(node):
        if node["_eff_wdist"] > 0:
            node["avg_eff"] = round(node["_eff_wsum"] / node["_eff_wdist"], 1)

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
        mo  = dt.strftime("%B %Y")
        day = dt.strftime("%d %b %Y")

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


def get_trip_detail(trip_id: int) -> Optional[dict]:
    db = _get()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        return None
    positions = db.execute(
        "SELECT latitude, longitude, speed_kmh, soc FROM trip_positions WHERE trip_id = ? ORDER BY id",
        (trip_id,),
    ).fetchall()
    trip_d = dict(trip)
    trip_d["started_at"] = _local_iso(trip_d.get("started_at"))
    trip_d["ended_at"] = _local_iso(trip_d.get("ended_at"))
    return {
        **trip_d,
        "positions": [dict(p) for p in positions],
    }


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
            ROUND(SUM(distance_km), 1)    AS total_km,
            ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km, 0) / 100), 1) AS total_kwh,
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

    years: dict = OrderedDict()
    for r in rows:
        d = dict(r)
        yr, mo_key, day_key = d["year"], d["month_key"], d["day_key"]

        # Format labels in Python (SQLite %B/%b not supported)
        try:
            mo_dt  = datetime.strptime(mo_key, "%Y-%m")
            mo_label = mo_dt.strftime("%B %Y")
            day_dt   = datetime.strptime(day_key, "%Y-%m-%d")
            d["day_label"] = day_dt.strftime("%d %b %Y")
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
            node["total_km"]         = round(node["total_km"] + km, 1)
            node["total_kwh"]        = round(node["total_kwh"] + (d.get("total_kwh") or 0), 1)
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
               ROUND(SUM(distance_km), 1)     AS total_km,
               ROUND(SUM(CASE WHEN efficiency_kwh_100km IS NOT NULL
                              THEN distance_km END), 1) AS km_with_eff,
               ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km,0) / 100), 1) AS total_kwh,
               ROUND(AVG(efficiency_kwh_100km), 1) AS avg_efficiency
           FROM trips
           WHERE ended_at IS NOT NULL
           GROUP BY month
           ORDER BY month DESC
           LIMIT 12""",
    ).fetchall()
    return [dict(r) for r in rows]


def get_charges_grouped() -> list[dict]:
    """Return charges nested as year → month → day."""
    charges = get_charges()
    from collections import OrderedDict

    def _node(label):
        return {"label": label, "count": 0, "kwh": 0.0, "cost": 0.0, "has_cost": False, "months": OrderedDict()}

    def _day_node(label):
        return {"label": label, "count": 0, "kwh": 0.0, "cost": 0.0, "has_cost": False, "charges": []}

    years: dict = OrderedDict()
    for c in charges:
        if not c.get("started_at"):
            continue
        dt = _local_dt(c["started_at"])
        if dt is None:
            continue
        c["started_at"] = dt.isoformat()
        c["ended_at"] = _local_iso(c.get("ended_at"))

        yr  = dt.strftime("%Y")
        mo  = dt.strftime("%B %Y")
        day = dt.strftime("%d %b %Y")

        years.setdefault(yr, _node(yr))
        years[yr]["months"].setdefault(mo, {**_node(mo), "days": OrderedDict()})
        years[yr]["months"][mo]["days"].setdefault(day, _day_node(day))

        years[yr]["months"][mo]["days"][day]["charges"].append(c)

        kwh  = c.get("energy_added_kwh") or 0
        cost = c.get("cost") or 0
        for node in [years[yr], years[yr]["months"][mo], years[yr]["months"][mo]["days"][day]]:
            node["kwh"]   = round(node["kwh"] + kwh, 1)
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
               ROUND(SUM(distance_km), 1)                                    AS total_km,
               ROUND(SUM(distance_km * COALESCE(efficiency_kwh_100km,0)/100), 1) AS total_kwh_used,
               ROUND(SUM(duration_min), 0)                                   AS total_drive_min,
               ROUND(AVG(efficiency_kwh_100km), 1)                           AS avg_efficiency,
               ROUND(MIN(efficiency_kwh_100km), 1)                           AS best_efficiency,
               ROUND(SUM(regen_kwh), 1)                                      AS total_regen_kwh,
               ROUND(AVG(regen_kwh), 2)                                      AS avg_regen_kwh
           FROM trips WHERE ended_at IS NOT NULL"""
    ).fetchone()
    charges = db.execute(
        """SELECT
               COUNT(*)                         AS charge_count,
               ROUND(SUM(energy_added_kwh), 1)  AS total_kwh_charged,
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
               ROUND(SUM(energy_added_kwh), 1)    AS total_kwh,
               ROUND(AVG(duration_min / 60.0), 1) AS avg_duration_h,
               ROUND(SUM(cost), 2)                AS total_cost,
               ROUND(AVG(end_soc - start_soc), 1) AS avg_soc_delta,
               ROUND(MAX(max_power_kw), 1)        AS peak_power_kw
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
    ac["kwh"] = round(ac["kwh"], 1)
    dc["kwh"] = round(dc["kwh"], 1)
    return {"ac": ac, "dc": dc, "total": ac["count"] + dc["count"]}
