"""REEV Phase C — per-trip fuel consumption. The poller records the fuel-tank % (signal 3235) at
trip start/end; the web layer derives litres burned (Δ% × 50 L tank) and L/100km. There's no
'engine on' PID — the range-extender ran iff the fuel level dropped. All inert on a BEV (no fuel).

Pure helper runs with no DB; the poller-capture tests use a tmp_path DB. CI-safe."""
import sqlite3
import types

import db as D
import db_reader


def _pos_db(rows):
    """In-memory positions table with (recorded_at, odometer_km, fuel_level_pct) for vehicle 1 —
    the only columns _reev_engine_on reads. No ambient DB (CI-safe)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, vehicle_id INT, "
                 "recorded_at TEXT, odometer_km REAL, fuel_level_pct REAL)")
    for i, (ts, odo, fuel) in enumerate(rows):
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?)", (i, 1, ts, odo, fuel))
    return conn


def _vd(fuel=None, soc=80.0, odo=1000.0):
    return types.SimpleNamespace(soc=soc, odometer_km=odo, latitude=45.0, longitude=9.0,
                                 fuel_level_pct=fuel)


# ── the derivation helper (pure) ──────────────────────────────────────────────
def test_engine_ran_gives_litres_and_l_per_100km():
    out = db_reader._reev_trip_fuel(98.4, 96.2, 17.6)   # gm27271's real engine-on trip
    assert out["engine_ran"] is True
    assert out["fuel_used_l"] == 1.1                    # (2.2/100) × 50 L
    assert out["fuel_l_100km"] == 6.2                   # 1.1 / 17.6 × 100 → ~6.2, matches on-car


def test_no_fuel_data_is_inert():
    assert db_reader._reev_trip_fuel(None, None, 20) == {
        "fuel_used_l": None, "fuel_l_100km": None, "engine_ran": False, "engine_km": None}


# ── the engine-on basis: L/100km over the generator-driving distance (matches the car) ─────────
def test_engine_on_segments_exclude_ev_and_stationary_charge():
    # A mixed trip: 10 km on the generator, 10 km pure-electric, then a stationary battery charge.
    conn = _pos_db([
        ("2026-07-07T20:00:00", 10.0, 96.0),
        ("2026-07-07T20:05:00", 20.0, 95.0),   # +10 km, −1.0%  → generator DRIVING (counts)
        ("2026-07-07T20:15:00", 30.0, 95.0),   # +10 km,  0%    → pure electric (excluded from km)
        ("2026-07-07T20:30:00", 30.0, 94.0),   # +0 km,  −1.0%  → stationary charge (excluded from litres)
    ])
    eng = db_reader._reev_engine_on(conn, 1, "2026-07-07T20:00:00", "2026-07-07T20:30:00")
    assert eng == {"engine_km": 10.0, "engine_fuel_pct": 1.0}


def test_engine_on_basis_matches_car_not_whole_trip():
    eng = {"engine_km": 10.0, "engine_fuel_pct": 1.0}       # from the trip above
    out = db_reader._reev_trip_fuel(96.0, 94.0, 30.0, eng)  # total drop 2.0% over the whole 30 km
    assert out["engine_ran"] is True
    assert out["engine_km"] == 10.0
    assert out["fuel_used_l"] == 1.0                        # total litres that left the tank (2.0% × 50)
    assert out["fuel_l_100km"] == 5.0                       # 1.0% × 50 / 10 km — generator-on basis, realistic
    # the OLD whole-trip method would have shown 1.0 L / 30 km = 3.3 → too low (diluted by the EV km)
    assert db_reader._reev_trip_fuel(96.0, 94.0, 30.0)["fuel_l_100km"] == 3.3


def test_falls_back_to_whole_trip_when_positions_pruned():
    # No engine trail (old trip) → keep the whole-trip distance so history doesn't break.
    out = db_reader._reev_trip_fuel(98.4, 96.2, 17.6, None)
    assert out["fuel_l_100km"] == 6.2 and out["engine_km"] is None


def test_engine_on_none_when_no_fuel_trail():
    conn = _pos_db([("2026-07-07T20:00:00", 10.0, None),
                    ("2026-07-07T20:05:00", 20.0, None)])   # BEV / no fuel column data
    assert db_reader._reev_engine_on(conn, 1, "2026-07-07T20:00:00", "2026-07-07T20:05:00") is None


def test_pure_electric_drive_engine_not_flagged():
    out = db_reader._reev_trip_fuel(80.0, 80.0, 20)     # fuel unchanged → engine didn't run
    assert out["engine_ran"] is False and out["fuel_used_l"] is None


def test_signal_noise_below_floor_ignored():
    out = db_reader._reev_trip_fuel(80.1, 80.0, 20)     # 0.1% = one signal tick, not a burn
    assert out["engine_ran"] is False


def test_short_trip_reports_litres_but_no_per_100km():
    out = db_reader._reev_trip_fuel(90.0, 88.0, 0.3)    # < 0.5 km → litres yes, L/100km withheld
    assert out["fuel_used_l"] == 1.0 and out["fuel_l_100km"] is None


# ── poller capture (create_trip / finalize_trip) ──────────────────────────────
def test_create_trip_stores_fuel_start(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    tid = db.create_trip(1, _vd(fuel=96.2))
    assert db._conn.execute("SELECT fuel_start_pct FROM trips WHERE id=?", (tid,)).fetchone()[0] == 96.2


def test_bev_trip_has_null_fuel(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    tid = db.create_trip(1, _vd(fuel=None))             # BEV — no fuel signal
    assert db._conn.execute("SELECT fuel_start_pct FROM trips WHERE id=?", (tid,)).fetchone()[0] is None


def test_finalize_trip_stores_fuel_end(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    db.set_battery_capacity(60.0)
    tid = db.create_trip(1, _vd(fuel=98.4, odo=1000.0))
    db.finalize_trip(tid, _vd(fuel=96.2, soc=75.0, odo=1017.6))
    row = db._conn.execute(
        "SELECT fuel_start_pct, fuel_end_pct FROM trips WHERE id=?", (tid,)).fetchone()
    assert row["fuel_start_pct"] == 98.4 and row["fuel_end_pct"] == 96.2


# ── REEV Phase D — per-trip ELECTRIC from getEC (beta #10 step 2) ──────────────────────────────

def test_reev_elec_from_getec_over_full_distance():
    # gm27271's real getEC driving energy (2.1 kWh) over the whole 19 km the motor drove.
    out = db_reader._reev_trip_elec(2.1, 19.0, True)
    assert out["reev_elec_kwh"] == 2.1
    assert out["reev_elec_kwh_100km"] == 11.1          # 2.1 / 19 * 100, over the FULL distance


def test_reev_elec_uses_full_distance_not_a_generator_subset():
    # Unlike fuel (normalised over the generator-on km), the motor drives the WHOLE trip → full distance.
    assert db_reader._reev_trip_elec(5.0, 40.0, True)["reev_elec_kwh_100km"] == 12.5


def test_reev_elec_inert_without_engine():
    # Pure-electric REEV trip (generator never ran) → this block doesn't apply.
    assert db_reader._reev_trip_elec(2.1, 19.0, False) == {"reev_elec_kwh": None, "reev_elec_kwh_100km": None}


def test_reev_elec_inert_without_getec():
    # BEV, or an engine-on trip the cloud hasn't enriched yet → no getEC → 'pending', not a fake number.
    assert db_reader._reev_trip_elec(None, 19.0, True) == {"reev_elec_kwh": None, "reev_elec_kwh_100km": None}


def test_reev_elec_zero_distance_is_safe():
    assert db_reader._reev_trip_elec(2.1, 0, True) == {"reev_elec_kwh": None, "reev_elec_kwh_100km": None}
