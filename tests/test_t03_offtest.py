"""TEMPORARY T03 climate command-probe candidates (#67). Verifies each item fires the exact payload we
intend (cmd 170 via ac_switch raw params, or the cmd-171 schedule) — so the tester's diagnostic shows the
right thing. Remove with web/t03_offtest.py once done. CI-safe: session stubbed."""
import json

import command_client
import t03_offtest


class _FakeApi:
    def __init__(self):
        self.calls = []

    def ac_switch(self, vin, *, params=None):
        self.calls.append(("ac_switch", params))
        return {"code": 0}

    def _remote_control_raw(self, *, vin, cmd_id, cmd_content, action_label):
        self.calls.append(("raw", cmd_id, json.loads(cmd_content)))
        return {"code": 0}


class _FakeSession:
    def __init__(self):
        self.api = _FakeApi()

    def execute(self, fn):
        fn(self.api, "VIN")
        return True, "ok"


def _stub(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(command_client, "_session", fake)
    return fake


def _items():
    return [it for sec in t03_offtest.SECTIONS for it in sec["items"]]


def test_every_item_fires_ok_and_has_unique_id(monkeypatch):
    fake = _stub(monkeypatch)
    ids = [it["id"] for it in _items()]
    assert len(ids) == len(set(ids))                     # no duplicate ids across sections
    for it in _items():
        ok, _ = t03_offtest.fire(it["id"])
        assert ok, it
    # exactly one cmd-171 (schedule) item; the rest are cmd-170 ac_switch
    assert sum(1 for k in fake.api.calls if k[0] == "raw") == 1
    assert sum(1 for k in fake.api.calls if k[0] == "ac_switch") == len(_items()) - 1


def test_off_baseline_is_operate_close(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(0)
    assert fake.api.calls[-1][1]["operate"] == "close"


def test_off_windlevel_zero_uses_manual(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(1)
    p = fake.api.calls[-1][1]
    assert p["operate"] == "manual" and p["windlevel"] == "0"


def test_off_probe_keys_present(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(3)
    assert fake.api.calls[-1][1].get("acSwitch") == "0"
    t03_offtest.fire(4)
    assert fake.api.calls[-1][1].get("enable") == "0"


def test_off_cmd171_schedule_on_zero(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(6)
    kind, cmd_id, body = fake.api.calls[-1]
    assert kind == "raw" and cmd_id == "171" and body["controls"][0]["on"] == "0"


def test_fan_probes_vary_windlevel_manual(monkeypatch):
    fake = _stub(monkeypatch)
    for cid, wl in ((10, "1"), (11, "4"), (12, "7")):
        t03_offtest.fire(cid)
        p = fake.api.calls[-1][1]
        assert p["operate"] == "manual" and p["windlevel"] == wl


def test_heat_probe_is_hot(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(13)
    p = fake.api.calls[-1][1]
    assert p["operate"] == "manual" and p["mode"] == "hot"


def test_recirc_probes_toggle_circle(monkeypatch):
    fake = _stub(monkeypatch)
    t03_offtest.fire(14)
    assert fake.api.calls[-1][1]["circle"] == "in"       # recirc ON
    t03_offtest.fire(15)
    assert fake.api.calls[-1][1]["circle"] == "out"      # fresh air


def test_unknown_item_is_safe(monkeypatch):
    _stub(monkeypatch)
    ok, msg = t03_offtest.fire(999)
    assert not ok and "unknown" in msg
