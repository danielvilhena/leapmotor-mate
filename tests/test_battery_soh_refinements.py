"""SoH trend refinements (db_reader.get_battery_health):
- cold charges are SHOWN but excluded from the figure (LFP reads low when cold);
- charges with car on (climate active) are SHOWN but excluded (distorted energy/SoC ratio);
- charges that end near 100% weigh more (the BMS recalibrates SoC there);
- each point carries battery temp + odometer (for the per-distance axis).
Seeds a poller DB and reads it back through the web db_reader, like test_capacity_override."""
import db as D
import db_reader


def _seed(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    db.set_battery_capacity(67.1)            # nominal reference for SoH/plausibility band
    return db


def _charge(db, cid, day, start_soc, end_soc, amps, temp, odo, climate_cooling=0):
    """One ended charge + 3 charging samples 15 min apart at 400 V × `amps`, so
    ∫V·I = (400·amps/1000)·0.5 h = 0.2·amps kWh of measured DC energy."""
    t0 = f"2026-{day}T08:00:00+00:00"
    t1 = f"2026-{day}T08:15:00+00:00"
    t2 = f"2026-{day}T08:30:00+00:00"
    db._conn.execute(
        "INSERT INTO charges (id,vehicle_id,started_at,ended_at,start_soc,end_soc,charge_type) "
        "VALUES (?,1,?,?,?,?,'AC')", (cid, t0, f"2026-{day}T08:31:00+00:00", start_soc, end_soc))
    for t in (t0, t1, t2):
        db._conn.execute(
            "INSERT INTO positions (vehicle_id,recorded_at,charging,charge_voltage_v,"
            "charge_current_a,battery_min_temp,odometer_km,climate_cooling) VALUES (1,?,1,400,?,?,?,?)",
            (t, amps, temp, odo, climate_cooling))
    db._conn.commit()


def test_cold_charge_is_shown_but_excluded(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    # both: ΔSoC 80, ∫V·I = 0.2·268 = 53.6 kWh → est = 53.6/0.8 = 67.0 kWh (in band)
    _charge(db, 1, "06-01", 20, 100, amps=268, temp=25, odo=1000)   # warm
    _charge(db, 2, "06-05", 20, 100, amps=268, temp=5,  odo=1500)   # cold (5°C < 15°C gate)
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))

    h = db_reader.get_battery_health()
    pts = {p["charge_id"]: p for p in h["points"]}
    assert pts[1]["excluded"] is False
    assert pts[2]["excluded"] is True and pts[2]["exclude_reason"] == "cold"
    assert h["sample_count"] == 1 and h["excluded_count"] == 1       # cold one out of the figure
    assert pts[2]["temp_c"] == 5.0 and pts[1]["odometer_km"] == 1000  # temp + odometer carried
    assert h["latest_capacity_kwh"] == 67.0                          # headline = the warm one only


def test_full_charges_weigh_more_in_headline(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    # X ends at 100% (weight 1.0), est 67.0 ; Y ends at 70% (weight 0.4), est 42/0.7 = 60.0
    _charge(db, 1, "06-01", 20, 100, amps=268, temp=25, odo=1000)
    _charge(db, 2, "06-05", 0,  70,  amps=210, temp=25, odo=2000)
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))

    h = db_reader.get_battery_health()
    assert h["sample_count"] == 2
    # plain mean would be 63.5; weighting toward the 100%-ender gives (67·1 + 60·0.4)/1.4 = 65.0
    assert h["latest_capacity_kwh"] == 65.0


def test_active_use_charge_is_shown_but_excluded(tmp_path, monkeypatch):
    """A charge with climate_on=1 during the session is distorted (car consumes part of the
    charged energy), so it must be excluded from the health figure but still appear in points."""
    db = _seed(tmp_path)
    _charge(db, 1, "06-01", 20, 100, amps=268, temp=25, odo=1000, climate_cooling=0)  # clean
    _charge(db, 2, "06-05", 20, 100, amps=268, temp=25, odo=1500, climate_cooling=1)  # A/C on
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))

    h = db_reader.get_battery_health()
    pts = {p["charge_id"]: p for p in h["points"]}
    assert pts[1]["excluded"] is False
    assert pts[2]["excluded"] is True and pts[2]["exclude_reason"] == "active_use"
    assert h["sample_count"] == 1 and h["active_use_count"] == 1
    assert h["latest_capacity_kwh"] == 67.0   # headline uses only the clean charge


def test_soc_jump_charge_is_shown_but_excluded(tmp_path, monkeypatch):
    """An AC charge where the BMS snapped the SoC upward mid-session inflates ΔSoC without
    real energy, making the capacity estimate too low. Such sessions must be excluded.

    Session design: 3 intervals of 15 min each at 400 V × 30 A (12 kW), plus a 1-min jump
    interval where SoC leaps +3% (3 %/min >> 0.8 threshold). This gives:
      energy  ≈ 3 + 0.2 + 3 = 6.2 kWh   (trapezoid, all gaps ≤ 15 min)
      ΔSoC    = 77→90 = 13%
      est     = 6.2 / 0.13 ≈ 47.7 kWh   within the 50–115% plausibility band
    Without the jump the real delta would be ~9% → est ≈ 69 kWh (close to nominal).
    """
    db = _seed(tmp_path)
    # Clean charge: SoC 20→100 (+80%), ∫V·I = 53.6 kWh → est 67.0 kWh
    _charge(db, 1, "06-01", 20, 100, amps=268, temp=25, odo=1000)
    # Charge with BMS jump — insert positions manually to control the SoC profile.
    t0 = "2026-06-05T08:00:00+00:00"   # soc 77 (normal start)
    t1 = "2026-06-05T08:15:00+00:00"   # soc 81 (+4% in 15 min, 0.27 %/min — fine)
    t2 = "2026-06-05T08:16:00+00:00"   # soc 84 (+3% in 1 min = 3 %/min — BMS snap!)
    t3 = "2026-06-05T08:31:00+00:00"   # soc 88 (+4% in 15 min — back to normal)
    db._conn.execute(
        "INSERT INTO charges (id,vehicle_id,started_at,ended_at,start_soc,end_soc,charge_type) "
        "VALUES (2,1,?,?,77,90,'AC')", (t0, "2026-06-05T08:32:00+00:00"))
    for t, soc in ((t0, 77.0), (t1, 81.0), (t2, 84.0), (t3, 88.0)):
        db._conn.execute(
            "INSERT INTO positions (vehicle_id,recorded_at,soc,charging,charge_voltage_v,"
            "charge_current_a,battery_min_temp,odometer_km) VALUES (1,?,?,1,400,30,25,1500)",
            (t, soc))
    db._conn.commit()
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))

    h = db_reader.get_battery_health()
    pts = {p["charge_id"]: p for p in h["points"]}
    assert pts[1]["excluded"] is False
    assert pts[2]["excluded"] is True and pts[2]["exclude_reason"] == "soc_jump"
    assert h["sample_count"] == 1 and h["soc_jump_count"] == 1


def test_cold_cutoff_setting_is_honoured(tmp_path, monkeypatch):
    """The Advanced 'cold cutoff' slider writes soh_temp_min_c; get_battery_health (called with
    no arg) reads it. Lower the cutoff below the charge's temp and the once-cold session counts."""
    db = _seed(tmp_path)
    _charge(db, 1, "06-01", 20, 100, amps=268, temp=25, odo=1000)   # warm
    _charge(db, 2, "06-05", 20, 100, amps=268, temp=5,  odo=1500)   # 5°C — excluded at the default 15
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))

    assert db_reader.get_battery_health()["excluded_count"] == 1     # default 15°C → cold one out

    db_reader.set_setting("soh_temp_min_c", "2")                     # slider: cutoff below 5°C
    h = db_reader.get_battery_health()
    assert h["temp_min_c"] == 2.0
    assert h["excluded_count"] == 0 and h["sample_count"] == 2       # both now count
    assert all(p["excluded"] is False for p in h["points"])
