"""energy_snapshots.maybe_sample — the phase-1 silent collector of the car's official lifetime
counters (totalEnergy incl. parked, integer kWh) + the getEC split over [previous snapshot, now].

Ledger contract under test: first row has no EC window ('first'); a second row only after 24h,
with the getEC window bound to EXACTLY the previous row's taken_at; a counters fetch failure
writes nothing and backs off 30 min instead of hammering every poll; a getEC miss must NOT lose
the counter reading (row written with NULL ec + 'miss'); 'empty' (genuinely no driving) stores
0.0 so aggregation needs no special case. No network — fake client, real tmp-path Database."""
import time
from datetime import datetime, timezone

import db as D
import energy_snapshots


class FakeClient:
    def __init__(self, counters=None, ec=("ok", {"driving": 3.1, "ac": 0.8, "other": 0.2})):
        self.counters = counters or {"total_energy_kwh": 663, "total_mileage_km": 4097.2}
        self.ec = ec
        self.ec_calls = []

    def get_energy_counters(self):
        return self.counters

    def get_ec_range(self, begin_ts, end_ts):
        self.ec_calls.append((begin_ts, end_ts))
        return self.ec


def _db(tmp_path):
    return D.Database(str(tmp_path / "t.db"))


def test_first_snapshot_has_no_ec_window(tmp_path):
    pdb, c = _db(tmp_path), FakeClient()
    energy_snapshots._failed_at = 0.0
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1") is True
    row = pdb.last_energy_snapshot("VIN1")
    assert row["total_energy_kwh"] == 663
    assert row["total_mileage_km"] == 4097.2
    assert row["ec_status"] == "first"
    assert row["ec_driving_kwh"] is None
    assert c.ec_calls == []  # a Δ needs two readings — nothing to query yet


def test_second_snapshot_only_after_24h_with_exact_window(tmp_path):
    pdb, c = _db(tmp_path), FakeClient()
    energy_snapshots._failed_at = 0.0
    t0 = time.time() - 25 * 3600
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1", now=t0) is True
    # 23h later: throttled
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1", now=t0 + 23 * 3600) is False
    # 25h later: sampled, EC window = [first row's taken_at, now] exactly
    t1 = t0 + 25 * 3600
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1", now=t1) is True
    row = pdb.last_energy_snapshot("VIN1")
    assert row["ec_status"] == "ok"
    assert row["ec_driving_kwh"] == 3.1
    assert c.ec_calls == [(int(t0), int(t1))]


def test_counters_failure_writes_nothing_and_backs_off(tmp_path):
    pdb = _db(tmp_path)
    c = FakeClient(counters={})
    c.counters = None
    energy_snapshots._failed_at = 0.0
    now = time.time()
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1", now=now) is False
    assert pdb.last_energy_snapshot("VIN1") is None
    # within the 30-min backoff a healthy client is NOT retried yet
    ok = FakeClient()
    assert energy_snapshots.maybe_sample(pdb, ok, "VIN1", now=now + 60) is False
    # after the backoff it is
    assert energy_snapshots.maybe_sample(pdb, ok, "VIN1", now=now + 1801) is True


def test_ec_miss_still_records_the_counter(tmp_path):
    """The counter reading is unrecoverable if skipped; a getEC gap is retro-queryable later."""
    pdb = _db(tmp_path)
    energy_snapshots._failed_at = 0.0
    t0 = time.time() - 25 * 3600
    energy_snapshots.maybe_sample(pdb, FakeClient(), "VIN1", now=t0)
    c = FakeClient(ec=("miss", None))
    assert energy_snapshots.maybe_sample(pdb, c, "VIN1", now=t0 + 25 * 3600) is True
    row = pdb.last_energy_snapshot("VIN1")
    assert row["ec_status"] == "miss"
    assert row["ec_driving_kwh"] is None
    assert row["total_energy_kwh"] == 663


def test_ec_empty_stores_zeros(tmp_path):
    pdb = _db(tmp_path)
    energy_snapshots._failed_at = 0.0
    t0 = time.time() - 25 * 3600
    energy_snapshots.maybe_sample(pdb, FakeClient(), "VIN1", now=t0)
    c = FakeClient(ec=("empty", None))
    energy_snapshots.maybe_sample(pdb, c, "VIN1", now=t0 + 25 * 3600)
    row = pdb.last_energy_snapshot("VIN1")
    assert row["ec_status"] == "empty"
    assert row["ec_driving_kwh"] == 0.0
    assert row["ec_ac_kwh"] == 0.0


def test_snapshots_are_keyed_by_vin(tmp_path):
    pdb = _db(tmp_path)
    energy_snapshots._failed_at = 0.0
    now = time.time()
    energy_snapshots.maybe_sample(pdb, FakeClient(), "VIN1", now=now)
    # a different car on the same DB is not throttled by VIN1's snapshot
    assert energy_snapshots.maybe_sample(pdb, FakeClient(), "VIN2", now=now) is True
    assert pdb.last_energy_snapshot("VIN1")["vin"] == "VIN1"
    assert pdb.last_energy_snapshot("VIN2")["vin"] == "VIN2"
