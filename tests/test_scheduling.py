"""Charge-schedule write must read-modify-write: change only enable/SoC/window and PRESERVE
the car's existing day mask (`cycles`), `circulation` and `recharge` — never guess them.
See command_client.save_charge_schedule.

Skipped where leapmotor_api isn't installed (the CI test env per pytest.ini); verified
in-container against the real client."""
import pytest

cc = pytest.importorskip("command_client", reason="needs leapmotor_api")


def test_charge_schedule_merge_preserves_car_fields(monkeypatch):
    captured = {}

    class FakeApi:
        def set_charge_schedule(self, vin, **kw):
            captured.update(kw)
            captured["vin"] = vin

    class FakeSession:
        def get_charge_schedule(self):
            # car has a custom day mask + circulation that the UI doesn't expose
            return {"chargeEnable": 0, "chargesoc": 80, "circulation": 1,
                    "cycles": "1,0,1,0,1,0,1", "endtime": "08:00",
                    "recharge": 0, "starttime": "22:00"}

        def execute(self, fn):
            fn(FakeApi(), "VINTEST")
            return True, "OK"

    monkeypatch.setattr(cc, "_session", FakeSession())
    ok, _ = cc.save_charge_schedule(enabled=True, soc_limit=90, start_time="23:30", end_time="07:00")
    assert ok
    # user-edited fields applied
    assert captured["enabled"] is True
    assert captured["soc_limit"] == 90
    assert captured["start_time"] == "23:30"
    assert captured["end_time"] == "07:00"
    # car-owned fields preserved, not clobbered/guessed
    assert captured["cycles"] == "1,0,1,0,1,0,1"
    assert captured["circulation"] == 1
    assert captured["recharge"] == 0


def test_charge_schedule_defaults_when_no_existing(monkeypatch):
    captured = {}

    class FakeApi:
        def set_charge_schedule(self, vin, **kw):
            captured.update(kw)

    class FakeSession:
        def get_charge_schedule(self):
            return {}  # car has no schedule yet

        def execute(self, fn):
            fn(FakeApi(), "VIN")
            return True, "OK"

    monkeypatch.setattr(cc, "_session", FakeSession())
    cc.save_charge_schedule(enabled=False, soc_limit=80, start_time="00:00", end_time="06:00")
    # falls back to all-days mask, never empty/None
    assert captured["cycles"] == "1,1,1,1,1,1,1"


def test_cycles_from_day_flags_position_order():
    # cycles is MONDAY-first (0=Mon..6=Sun), confirmed on-car 2026-06-07. The helper is
    # weekday-agnostic — it just maps flags[i] -> field i.
    assert cc.cycles_from_day_flags([True] * 7) == "1,1,1,1,1,1,1"
    # positions 1,2 set → Tue,Wed (Mon-first)
    assert cc.cycles_from_day_flags([False, True, True, False, False, False, False]) == "0,1,1,0,0,0,0"
    # only Monday (position 0)
    assert cc.cycles_from_day_flags([True, False, False, False, False, False, False]) == "1,0,0,0,0,0,0"
    # empty selection coerces to all-days (a window with no days would never fire)
    assert cc.cycles_from_day_flags([False] * 7) == "1,1,1,1,1,1,1"


def test_day_flags_from_cycles_roundtrip():
    assert cc.day_flags_from_cycles("0,1,1,0,0,0,0") == [False, True, True, False, False, False, False]
    assert cc.day_flags_from_cycles("1,1,1,1,1,1,1") == [True] * 7
    # tolerant of short/garbage input — missing positions are False, never an IndexError
    assert cc.day_flags_from_cycles("1,1") == [True, True, False, False, False, False, False]
    assert cc.day_flags_from_cycles("") == [False] * 7


def test_save_charge_schedule_uses_provided_cycles(monkeypatch):
    captured = {}

    class FakeApi:
        def set_charge_schedule(self, vin, **kw):
            captured.update(kw)

    class FakeSession:
        def get_charge_schedule(self):
            return {"cycles": "1,1,1,1,1,1,1", "circulation": 1, "recharge": 0}

        def execute(self, fn):
            fn(FakeApi(), "VIN")
            return True, "OK"

    monkeypatch.setattr(cc, "_session", FakeSession())
    # an explicit cycles arg (user picked days) must WIN over the car's current mask
    cc.save_charge_schedule(enabled=True, soc_limit=80, start_time="23:00",
                            end_time="07:00", cycles="0,1,1,1,1,1,0")
    assert captured["cycles"] == "0,1,1,1,1,1,0"  # weekdays only, not the car's all-days
    assert captured["circulation"] == 1            # still preserved
