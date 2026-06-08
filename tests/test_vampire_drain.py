"""get_vampire_drain: SoC lost while PARKED and NOT charging, from the per-poll positions log.
Windows are bounded by driving (speed OR an odometer rise) or charging; short/tiny drops are dropped.
Pure db_reader (no fastapi) → runs in CI."""
import sqlite3
from datetime import timezone

import db_reader

BIG = 100000  # lookback_days huge so the test rows are never filtered by the recency cutoff


def _setup(monkeypatch, rows):
    monkeypatch.setattr(db_reader, "_LOCAL_TZ", timezone.utc)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE positions (recorded_at TEXT, soc REAL, charging INT, "
                "speed_kmh REAL, odometer_km REAL)")
    con.executemany("INSERT INTO positions VALUES (?,?,?,?,?)", rows)
    con.commit()
    monkeypatch.setattr(db_reader, "_get", lambda: con)


def P(hhmm, soc, charging=0, speed=0, odo=1000.0):
    return (f"2026-06-08T{hhmm}:00+00:00", soc, charging, speed, odo)


def test_basic_parked_drain(monkeypatch):
    # parked & unplugged, SoC 80→77 over 6h → one window, 3% / 6h = 12 %/day
    _setup(monkeypatch, [P("00:00", 80), P("02:00", 79), P("04:00", 78), P("06:00", 77)])
    out = db_reader.get_vampire_drain(lookback_days=BIG)
    assert out["count"] == 1
    w = out["windows"][0]
    assert (w["drop_pct"], w["hours"], w["pct_per_day"]) == (3.0, 6.0, 12.0)
    assert out["typical_pct_per_day"] == 12.0


def test_driving_breaks_window_and_is_not_counted(monkeypatch):
    # park A (80→78, 4h) · a drive (speed>0, consumes 78→77) · park B (77→75, 3h)
    _setup(monkeypatch, [
        P("00:00", 80, odo=1000), P("04:00", 78, odo=1000),
        P("04:30", 77, speed=40, odo=1010),                 # driving → breaks A, not counted
        P("05:00", 77, odo=1010), P("08:00", 75, odo=1010),
    ])
    out = db_reader.get_vampire_drain(lookback_days=BIG)
    assert out["count"] == 2
    drops = sorted(w["drop_pct"] for w in out["windows"])
    assert drops == [2.0, 2.0]                              # the 1% driving loss is excluded


def test_odometer_jump_breaks_window(monkeypatch):
    # park A (80→79, 3h) · GAP with a drive (odo +50, no speed sample, 79→70) · park B (70→69, 3h)
    _setup(monkeypatch, [
        P("00:00", 80, odo=1000), P("03:00", 79, odo=1000),
        P("09:00", 70, odo=1050), P("12:00", 69, odo=1050),  # odo jumped → a drive happened → break
    ])
    out = db_reader.get_vampire_drain(lookback_days=BIG)
    assert out["count"] == 2
    # the 9% lost to the (unsampled) drive must NOT appear as a window
    assert all(w["drop_pct"] <= 1.5 for w in out["windows"])


def test_short_tiny_and_charging_are_excluded(monkeypatch):
    _setup(monkeypatch, [
        P("00:00", 80), P("00:30", 79),                     # 30 min < 1h → excluded
        P("10:00", 60, charging=1), P("12:00", 70, charging=1),  # charging → not a park
        P("20:00", 50), P("23:00", 49.7),                   # 0.3% drop < 0.5 → jitter, excluded
    ])
    out = db_reader.get_vampire_drain(lookback_days=BIG)
    assert out["count"] == 0 and out["typical_pct_per_day"] is None
