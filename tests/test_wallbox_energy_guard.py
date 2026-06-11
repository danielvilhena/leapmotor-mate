"""Physical guard on wallbox session energy (GitHub #46). A wallbox kWh counter can read ~0 at
plug-in and then snap back to its LIFETIME total, so the per-poll delta becomes a single absurd
step (tens of thousands of kWh for a 15-minute charge) that inflated both the energy shown and the
cost. Three layers, all in poller/db: a per-poll guard (skip the impossible step, keep counting the
real rises after it), a finalize backstop (drop a still-impossible total → DC billing), and a
one-time repair for rows already in the DB. Pure poller.db → runs in CI."""
import types
from datetime import datetime, timedelta, timezone

import db as D


def _at(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_accumulate_skips_implausible_jump_then_self_corrects(tmp_path):
    """A counter that reads ~0 at start then jumps to its lifetime total: the jump is ignored,
    the baseline advances, and the real rises AFTER it are still summed → the session recovers."""
    db = D.Database(str(tmp_path / "t.db"))
    cid = db.create_charge(1, types.SimpleNamespace(soc=38, latitude=1.0, longitude=2.0))
    db.set_charge_wallbox_start(cid, 0.0)            # entity read 0 at plug-in
    db.accumulate_wallbox_energy(cid, 10570.0)       # snaps to lifetime total → impossible step
    db.accumulate_wallbox_energy(cid, 10570.4)       # +0.4 real
    db.accumulate_wallbox_energy(cid, 10570.7)       # +0.3 real
    ac = db._conn.execute("SELECT ac_energy_kwh FROM charges WHERE id=?", (cid,)).fetchone()[0]
    assert ac == 0.7                                  # the 10,570 jump never counted


def test_finalize_drops_still_impossible_total(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    cid = db.create_charge(1, types.SimpleNamespace(soc=38, latitude=1.0, longitude=2.0))
    # A bogus total that bypassed the per-poll guard, on a 15-minute session.
    db._conn.execute("UPDATE charges SET started_at=?, ac_energy_kwh=10570.0 WHERE id=?",
                     (_at(minutes=15), cid))
    db._conn.commit()
    db.finalize_charge(cid, types.SimpleNamespace(soc=40, latitude=1.0, longitude=2.0),
                       max_power_kw=3.1)
    ac = db._conn.execute("SELECT ac_energy_kwh FROM charges WHERE id=?", (cid,)).fetchone()[0]
    assert ac is None                                 # dropped → charge bills on DC energy


def test_finalize_keeps_plausible_total(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    cid = db.create_charge(1, types.SimpleNamespace(soc=66, latitude=1.0, longitude=2.0))
    db._conn.execute("UPDATE charges SET started_at=?, ac_energy_kwh=6.7 WHERE id=?",
                     (_at(hours=3), cid))
    db._conn.commit()
    db.finalize_charge(cid, types.SimpleNamespace(soc=84, latitude=1.0, longitude=2.0),
                       max_power_kw=3.2)
    ac = db._conn.execute("SELECT ac_energy_kwh FROM charges WHERE id=?", (cid,)).fetchone()[0]
    assert ac == 6.7                                  # real wallbox energy untouched


def test_repair_cleans_bogus_and_rescales_cost(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    con = db._conn
    cols = ("vehicle_id, location_type, started_at, ended_at, start_soc, end_soc, "
            "energy_added_kwh, ac_energy_kwh, cost, max_power_kw, duration_min")
    # Bogus HOME charge: 10,570 kWh AC for a 15-min session, cost billed on it (~0.32 €/kWh).
    con.execute(f"INSERT INTO charges (id, {cols}) VALUES (1,1,'HOME',?,?,38,40,0.7,10570.0,3382.40,3.1,15)",
                (_at(minutes=15), _at(minutes=0)))
    # Real HOME charge: 6.7 kWh AC over ~3h — must be left alone.
    con.execute(f"INSERT INTO charges (id, {cols}) VALUES (2,1,'HOME',?,?,66,84,10.0,6.7,2.15,3.2,176)",
                (_at(hours=3), _at(minutes=0)))
    con.execute("DELETE FROM settings WHERE key='charges_wb_energy_repair_v1'")  # let it run again
    con.commit()

    db._repair_bogus_wallbox_energy()

    bad = con.execute("SELECT ac_energy_kwh, cost FROM charges WHERE id=1").fetchone()
    good = con.execute("SELECT ac_energy_kwh, cost FROM charges WHERE id=2").fetchone()
    assert bad["ac_energy_kwh"] is None
    assert bad["cost"] == round(3382.40 / 10570.0 * 0.7, 2)   # 0.22 — rescaled to DC at same €/kWh
    assert good["ac_energy_kwh"] == 6.7                       # untouched
    assert good["cost"] == 2.15


def test_repair_is_idempotent(tmp_path):
    db = D.Database(str(tmp_path / "t.db"))
    con = db._conn
    con.execute("INSERT INTO charges (id, vehicle_id, location_type, started_at, ended_at, "
                "start_soc, end_soc, energy_added_kwh, ac_energy_kwh, cost, max_power_kw, duration_min) "
                "VALUES (1,1,'HOME',?,?,38,40,0.7,10570.0,3382.40,3.1,15)",
                (_at(minutes=15), _at(minutes=0)))
    con.execute("DELETE FROM settings WHERE key='charges_wb_energy_repair_v1'")
    con.commit()
    db._repair_bogus_wallbox_energy()
    db._repair_bogus_wallbox_energy()                 # second run is a no-op (flag set)
    assert db.get_setting("charges_wb_energy_repair_v1") == "1"
