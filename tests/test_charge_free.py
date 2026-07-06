"""#120: mark a HOME charge as FREE (self-produced solar, or any free home charge). Mate can't
tell solar from grid (no metering behind the meter), so this is a user declaration — the charge
KEEPS its Home location (stays on the Home side of the Home-vs-Public split, unlike the FREE
location_type which is 'free away') and its cost is pinned to 0, protected from every recompute.

Runs on a tmp_path DB (poller schema — which also runs the is_free migration) with db_reader
pointed at it. CI-safe."""
import db as D
import db_reader


def _setup(tmp_path, monkeypatch):
    pdb = D.Database(str(tmp_path / "t.db"))
    pdb.set_battery_capacity(67.1)
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "t.db"))
    db_reader.set_setting("price_home_kwh", "0.25")
    db_reader.set_setting("price_ac_kwh", "0.40")
    return pdb


def _charge(pdb, cid, *, ctype=None, ac=None):
    pdb._conn.execute(
        "INSERT INTO charges (id, vehicle_id, started_at, ended_at, start_soc, end_soc,"
        " energy_added_kwh, ac_energy_kwh, location_type)"
        " VALUES (?,1,'2026-06-02T16:48:39+00:00','2026-06-02T18:18:36+00:00',40,52,8.0,?,?)",
        (cid, ac, ctype))
    pdb._conn.commit()


def _row(pdb, cid):
    return pdb._conn.execute("SELECT * FROM charges WHERE id=?", (cid,)).fetchone()


# ── migration ────────────────────────────────────────────────────────────────
def test_migration_adds_is_free_column(tmp_path):
    pdb = D.Database(str(tmp_path / "t.db"))
    cols = {r[1] for r in pdb._conn.execute("PRAGMA table_info(charges)").fetchall()}
    assert "is_free" in cols


# ── compute_cost is authoritative ─────────────────────────────────────────────
def test_compute_cost_returns_zero_when_free():
    assert db_reader.compute_cost({"location_type": "HOME", "energy_added_kwh": 8.0, "is_free": 1}) == 0.0


def test_compute_cost_missing_is_free_key_is_safe():
    # a dict without the key must not raise (plain-dict callers / old rows)
    out = db_reader.compute_cost({"location_type": "HOME", "energy_added_kwh": 8.0})
    assert out != 0.0    # not forced free


# ── set_charge_free on HOME ───────────────────────────────────────────────────
def test_mark_home_free_keeps_home_and_zeroes_cost(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="HOME")
    priced = db_reader.update_charge_type(1, "HOME")
    assert priced["cost"] == 2.0 and priced["is_free"] == 0      # 8 kWh × 0.25

    freed = db_reader.set_charge_free(1, True)
    assert freed["location_type"] == "HOME"                      # STILL home (not FREE type)
    assert freed["is_free"] == 1
    assert freed["cost"] == 0.0


def test_unmark_recomputes_home_cost(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="HOME")
    db_reader.update_charge_type(1, "HOME")
    db_reader.set_charge_free(1, True)
    back = db_reader.set_charge_free(1, False)
    assert back["is_free"] == 0
    assert back["cost"] == 2.0


def test_free_is_home_only_noop_on_public(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="AC")
    out = db_reader.set_charge_free(1, True)
    assert not out.get("is_free")                                # unchanged — free is HOME-only


def test_switching_type_away_from_home_drops_free(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="HOME")
    db_reader.update_charge_type(1, "HOME")
    db_reader.set_charge_free(1, True)
    switched = db_reader.update_charge_type(1, "AC")             # re-tag to a public type
    assert switched["is_free"] == 0
    assert switched["cost"] == 3.2                               # 8 kWh × 0.40 (AC), recomputed


# ── the whole point: a free home charge counts in Home, at 0, NOT in Public ────
def test_free_home_charge_lands_in_home_bucket_not_public(tmp_path, monkeypatch):
    pdb = _setup(tmp_path, monkeypatch)
    _charge(pdb, 1, ctype="HOME")
    db_reader.set_charge_free(1, True)
    buckets = db_reader._collect_monthly_buckets()
    b = next(v for v in buckets.values() if v["charge_count"] > 0)
    assert b["home"]["count"] == 1
    assert b["home"]["cost"] == 0.0
    assert b["public"]["count"] == 0        # NOT lumped into Public — the #120 fix
    assert b["charge_kwh"] > 0              # energy still counted in totals
