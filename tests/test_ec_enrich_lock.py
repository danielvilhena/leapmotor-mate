"""Per-trip EC (getEC) enrichment lock — the part that decides when a trip's cloud energy
split is considered FINAL (ec_stable=1) and overrides the SoC efficiency.

Regression for the 28/06 field bug: trips 166/167 didn't lock on their own and had to be
locked by hand. Root fragility (reproduced below as scenario C): the cloud quantizes EC to
0.1 kWh, so a value wobbling ONE step across a rounding boundary (1.9↔2.0) never satisfied the
old 0.05-abs "two equal reads" rule → it never locked. The fix: a 0.15/5% convergence tolerance
plus an age backstop that GUARANTEES an autonomous lock once the trip is old enough.

No network, no real settings DB — a tmp_path poller DB with db_reader pointed at it, and the
cloud call stubbed to a scripted sequence of reads.
"""
from datetime import datetime, timedelta, timezone

import pytest

import db as D            # poller schema (trips/settings + migrations)
import db_reader
import ec_enrich
import command_client


def _setup(tmp_path, monkeypatch, *, age_min):
    """One finalized trip ended `age_min` ago, feature enabled, cutoff before it."""
    pdb = D.Database(str(tmp_path / "t.db"))
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))
    now = datetime.now(timezone.utc)
    ended = now - timedelta(minutes=age_min)
    started = ended - timedelta(minutes=5)
    pdb._conn.execute(
        "INSERT INTO trips (id, vehicle_id, started_at, ended_at, distance_km,"
        " efficiency_kwh_100km) VALUES (1, 1, ?, ?, 7.0, 30.0)",
        (started.isoformat(), ended.isoformat()))
    pdb._conn.commit()
    db_reader.set_setting("ec_trip_energy_enabled", "1")
    db_reader.set_setting("ec_trip_since", (started - timedelta(hours=1)).isoformat())
    return pdb


def _ec(total):
    """A getEC reading shaped like get_energy_breakdown_range's output, or None for a miss."""
    if total is None:
        return None
    return {"driving_kwh": round(total * 0.42, 1), "ac_kwh": round(total * 0.47, 1),
            "other_kwh": round(total * 0.11, 1), "total_kwh": total,
            "driving_pct": 42.0, "ac_pct": 47.0, "other_pct": 11.0}


def _row(pdb):
    return pdb._conn.execute(
        "SELECT ec_kwh, ec_stable, ec_tried, efficiency_kwh_100km, efficiency_soc "
        "FROM trips WHERE id=1").fetchone()


def _run(pdb, monkeypatch, reads):
    """Feed `reads` one per sweep; return the 1-based step at which ec_stable first became 1
    (or None if it never locked)."""
    locked_at = None
    for i, v in enumerate(reads, 1):
        monkeypatch.setattr(command_client, "get_energy_breakdown_range", lambda b, e, _v=v: _ec(_v))
        ec_enrich._sweep_now()
        if locked_at is None and _row(pdb)[1] == 1:
            locked_at = i
    return locked_at


# ── the lock must complete on its own in every realistic read pattern ─────────

def test_constant_value_locks_on_second_read(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    assert _run(pdb, monkeypatch, [1.9, 1.9]) == 2


def test_none_between_reads_does_not_reset(tmp_path, monkeypatch):
    """A transient cloud miss bumps ec_tried but keeps the stored value → still locks."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    assert _run(pdb, monkeypatch, [1.9, None, 1.9]) == 3


def test_one_step_boundary_wobble_locks(tmp_path, monkeypatch):
    """THE field bug (166/167): value bounces one 0.1 quantization step. Must converge & lock,
    not spin forever as it did with the old 0.05 tolerance."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    assert _run(pdb, monkeypatch, [1.9, 2.0, 1.9, 2.0]) == 2


def test_wild_oscillation_locks_via_backstop(tmp_path, monkeypatch):
    """Values too far apart to converge still lock via the age backstop (2nd usable read, old
    enough) — enrichment can never get permanently stuck."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    assert _run(pdb, monkeypatch, [1.5, 2.5, 1.5, 2.5]) == 2


def test_young_trip_never_locks_early(tmp_path, monkeypatch):
    """Below the 30-min maturity gate nothing locks, even with identical reads — a still-aggregating
    cloud value must not be frozen (and the efficiency must not be overridden)."""
    pdb = _setup(tmp_path, monkeypatch, age_min=10)
    assert _run(pdb, monkeypatch, [1.9, 1.9, 1.9]) is None
    assert _row(pdb)[3] == pytest.approx(30.0)   # SoC efficiency untouched
    assert _row(pdb)[4] is None                  # no backup taken


# ── manual on-demand conversion (the "Convert with official data" button) ─────

def test_convert_trip_applies_official_even_with_feature_off(tmp_path, monkeypatch):
    """Manual convert is an explicit user action: it ignores the feature flag and the age/maturity
    gates, locks immediately, overrides efficiency and keeps SoC as backup."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    db_reader.set_setting("ec_trip_energy_enabled", "0")   # off → background sweep wouldn't touch it
    monkeypatch.setattr(command_client, "get_energy_breakdown_range",
                        lambda b, e: _ec(1.9))
    res = ec_enrich.convert_trip(1)
    assert res["ok"] is True
    ec_kwh, stable, _tried, eff, eff_soc = _row(pdb)
    assert stable == 1 and ec_kwh == pytest.approx(1.9)
    assert eff == pytest.approx(27.1, abs=0.05)
    assert eff_soc == pytest.approx(30.0)


def test_convert_trip_no_cloud_data_changes_nothing(tmp_path, monkeypatch):
    """Old/unresolved trip: the cloud has no data → returns no_data and leaves the trip on SoC."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    monkeypatch.setattr(command_client, "get_energy_breakdown_range", lambda b, e: None)
    res = ec_enrich.convert_trip(1)
    assert res["ok"] is False and res["reason"] == "no_data"
    ec_kwh, stable, _tried, eff, _soc = _row(pdb)
    assert stable == 0 and ec_kwh is None and eff == pytest.approx(30.0)


def _insert_prev(pdb, *, gap_min):
    """Insert a previous trip (id 2) that ended `gap_min` before trip 1 started."""
    from datetime import datetime, timedelta
    s1 = datetime.fromisoformat(pdb._conn.execute("SELECT started_at FROM trips WHERE id=1").fetchone()[0])
    pe = s1 - timedelta(minutes=gap_min)
    ps = pe - timedelta(minutes=10)
    pdb._conn.execute(
        "INSERT INTO trips (id, vehicle_id, started_at, ended_at, distance_km, efficiency_kwh_100km) "
        "VALUES (2,1,?,?,4.0,20.0)", (ps.isoformat(), pe.isoformat()))
    pdb._conn.commit()


def test_convert_no_data_brief_stop_reports_merged(tmp_path, monkeypatch):
    """No cloud data AND the previous trip ended a brief moment before (< merge default) → one drive
    the cloud merged; suggest merging (distinct reason/message), not the generic 'no data'."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    _insert_prev(pdb, gap_min=2)   # momentary stop → one continuous drive
    monkeypatch.setattr(command_client, "get_energy_breakdown_range", lambda b, e: None)
    assert ec_enrich.convert_trip(1)["reason"] == "merged_cloud"


def test_convert_no_data_real_stop_reports_no_data(tmp_path, monkeypatch):
    """A real stop (≥ merge default — shopping, errands) = two separate trips → genuine 'no data',
    NOT a merge suggestion (merging distinct trips makes no sense)."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    _insert_prev(pdb, gap_min=20)   # 20-min stop = a destination, not one drive
    monkeypatch.setattr(command_client, "get_energy_breakdown_range", lambda b, e: None)
    assert ec_enrich.convert_trip(1)["reason"] == "no_data"


# ── merge two close trips → convert the COMBINED drive ────────────────────────

def _two_close_trips(tmp_path, monkeypatch):
    """Trip 1 (A, 4 km, 20 kWh/100) then trip 2 (B, 6 km) starting 1 min after A — mergeable."""
    from datetime import datetime, timedelta, timezone
    pdb = D.Database(str(tmp_path / "t.db"))
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))
    now = datetime.now(timezone.utc)
    a_end = now - timedelta(minutes=140)
    a_start = a_end - timedelta(minutes=10)
    b_start = a_end + timedelta(minutes=1)
    b_end = b_start + timedelta(minutes=15)
    pdb._conn.execute("INSERT INTO trips (id,vehicle_id,started_at,ended_at,distance_km,"
                      "efficiency_kwh_100km,start_soc,end_soc) VALUES (1,1,?,?,4.0,20.0,80,78)",
                      (a_start.isoformat(), a_end.isoformat()))
    pdb._conn.execute("INSERT INTO trips (id,vehicle_id,started_at,ended_at,distance_km,"
                      "efficiency_kwh_100km,start_soc,end_soc) VALUES (2,1,?,?,6.0,25.0,78,75)",
                      (b_start.isoformat(), b_end.isoformat()))
    pdb._conn.commit()
    db_reader.set_setting("ec_trip_energy_enabled", "1")
    db_reader.set_setting("ec_trip_since", (a_start - timedelta(hours=1)).isoformat())
    return pdb


def test_merge_then_convert_uses_combined_drive(tmp_path, monkeypatch):
    pdb = _two_close_trips(tmp_path, monkeypatch)
    assert db_reader.merge_trips(1, 2)["ok"] is True
    monkeypatch.setattr(command_client, "get_energy_breakdown_range",
                        lambda b, e: {"driving_kwh": 1.0, "ac_kwh": 0.4, "other_kwh": 0.2,
                                      "total_kwh": 1.6, "driving_pct": 62, "ac_pct": 25, "other_pct": 13})
    assert ec_enrich.convert_trip(1)["ok"] is True
    det = db_reader.get_trip_detail(1)
    assert det["distance_km"] == pytest.approx(10.0)          # combined, not 4
    assert det["ec_kwh"] == pytest.approx(1.6)
    assert det["efficiency_kwh_100km"] == pytest.approx(16.0)  # 1.6 / 10 km, official — not SoC
    assert det["energy_kwh"] == pytest.approx(1.6, abs=0.05)


def test_convert_first_of_brief_split_suggests_merge_not_overattribute(tmp_path, monkeypatch):
    """Converting the EARLIER half of a brief split standalone: the cloud returns the COMBINED energy on
    that trip's own window — applying it over only its distance would overstate it (1.6/4km=40 vs the
    correct 1.6/10km=16). Guard must suggest merge and leave the trip untouched."""
    pdb = _two_close_trips(tmp_path, monkeypatch)   # trip 1 then trip 2, 1 min later (mergeable)
    monkeypatch.setattr(command_client, "get_energy_breakdown_range",
                        lambda b, e: {"driving_kwh": 1.0, "ac_kwh": 0.3, "other_kwh": 0.3,
                                      "total_kwh": 1.6, "driving_pct": 62, "ac_pct": 19, "other_pct": 19})
    res = ec_enrich.convert_trip(1)
    assert res["ok"] is False and res["reason"] == "merged_cloud"
    r = pdb._conn.execute("SELECT ec_kwh, ec_stable, round(efficiency_kwh_100km, 1) FROM trips WHERE id=1").fetchone()
    assert r[0] is None and r[1] == 0 and r[2] == 20.0   # untouched — NOT over-attributed to 40


def test_unmerge_clears_combined_ec_and_restores_soc(tmp_path, monkeypatch):
    pdb = _two_close_trips(tmp_path, monkeypatch)
    db_reader.merge_trips(1, 2)
    monkeypatch.setattr(command_client, "get_energy_breakdown_range",
                        lambda b, e: {"driving_kwh": 1.0, "ac_kwh": 0.4, "other_kwh": 0.2,
                                      "total_kwh": 1.6, "driving_pct": 62, "ac_pct": 25, "other_pct": 13})
    ec_enrich.convert_trip(1)
    db_reader.unmerge_trip(1)
    a = db_reader.get_trip_detail(1)
    assert a["ec_kwh"] is None and a["ec_stable"] == 0
    assert a["efficiency_kwh_100km"] == pytest.approx(20.0)   # A's own SoC efficiency restored


def test_lock_overrides_efficiency_and_backs_up_soc(tmp_path, monkeypatch):
    """On lock the trip's efficiency becomes the EC-derived figure and the SoC value is preserved
    for a reversible revert."""
    pdb = _setup(tmp_path, monkeypatch, age_min=120)
    assert _run(pdb, monkeypatch, [1.9, 1.9]) == 2
    ec_kwh, stable, _tried, eff, eff_soc = _row(pdb)
    assert stable == 1 and ec_kwh == pytest.approx(1.9)
    assert eff == pytest.approx(27.1, abs=0.05)   # 1.9 / 7 km * 100
    assert eff_soc == pytest.approx(30.0)          # original SoC efficiency kept as backup
