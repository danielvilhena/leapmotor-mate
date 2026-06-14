"""MANUAL charge cost: the user-entered total actually paid OVERRIDES the automatic cost, and the
automatic costers (auto-confirm + the one-time repairs) leave a MANUAL charge's cost alone — while
still feeding the WAC like any priced charge. Runs on a tmp_path DB (poller schema + db_reader
pointed at it) — no settings DB, CI-safe."""
import db as D            # poller schema (creates charges/positions/settings + migrations)
import db_reader


def _setup(tmp_path, monkeypatch):
    pdb = D.Database(str(tmp_path / "t.db"))
    pdb.set_battery_capacity(60.0)
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))
    return pdb


def _charge(pdb, cid, *, start_soc=40, end_soc=80, energy=24.0, cost=None, ctype=None,
            ac=None, started="2026-06-02T16:48:39+00:00", ended="2026-06-02T21:18:36+00:00"):
    pdb._conn.execute(
        "INSERT INTO charges (id, vehicle_id, started_at, ended_at, start_soc, end_soc,"
        " energy_added_kwh, ac_energy_kwh, location_type, cost, reconstructed)"
        " VALUES (?,1,?,?,?,?,?,?,?,?,0)",
        (cid, started, ended, start_soc, end_soc, energy, ac, ctype, cost))
    pdb._conn.commit()


def _row(pdb, cid):
    return pdb._conn.execute("SELECT * FROM charges WHERE id=?", (cid,)).fetchone()


def test_manual_cost_overrides_estimate(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="FAST", cost=12.0)                       # Mate's table estimate
    out = db_reader.update_charge_type(1, "MANUAL", manual_cost=18.45)
    assert out["location_type"] == "MANUAL"
    assert out["cost"] == 18.45


def test_manual_accepts_comma_via_caller(tmp_path, monkeypatch):
    # update_charge_type takes a float; the endpoint normalises "18,45" → 18.45 before calling.
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1)
    assert db_reader.update_charge_type(1, "MANUAL", manual_cost=float("18.45"))["cost"] == 18.45


def test_manual_retag_without_amount_keeps_cost(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="MANUAL", cost=18.45)
    out = db_reader.update_charge_type(1, "MANUAL", manual_cost=None)
    assert out["cost"] == 18.45


def test_switching_away_from_manual_recomputes(tmp_path, monkeypatch):
    # Leaving MANUAL for a real type drops the manual € and re-derives from the type (here: no
    # price set → None), confirming MANUAL isn't sticky once you pick a computed type.
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="MANUAL", cost=18.45)
    out = db_reader.update_charge_type(1, "FREE")          # FREE = explicit 0.0
    assert out["location_type"] == "FREE"
    assert out["cost"] == 0.0


def test_snap_to_full_repair_keeps_manual_cost(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    # MANUAL charge ending at 100% with over-counted energy; a real charging sample at 90%.
    _charge(pdb, 1, start_soc=50, end_soc=100, energy=30.0, cost=25.0, ctype="MANUAL")
    pdb._conn.execute(
        "INSERT INTO positions (vehicle_id, recorded_at, soc, charging)"
        " VALUES (1, '2026-06-02T20:00:00+00:00', 90.0, 1)")
    pdb.set_setting("charges_soc_snap_repair_v1", "")     # let the one-time repair run again
    pdb._conn.commit()
    pdb._repair_snap_to_full_charges()
    r = _row(pdb, 1)
    assert r["cost"] == 25.0                               # manual € untouched
    assert r["energy_added_kwh"] < 30.0                    # energy still corrected (50→90, not →100)
