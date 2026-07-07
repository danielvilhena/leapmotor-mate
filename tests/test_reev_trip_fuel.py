"""REEV Phase C — per-trip fuel consumption. The poller records the fuel-tank % (signal 3235) at
trip start/end; the web layer derives litres burned (Δ% × 50 L tank) and L/100km. There's no
'engine on' PID — the range-extender ran iff the fuel level dropped. All inert on a BEV (no fuel).

Pure helper runs with no DB; the poller-capture tests use a tmp_path DB. CI-safe."""
import types

import db as D
import db_reader


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
        "fuel_used_l": None, "fuel_l_100km": None, "engine_ran": False}


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
