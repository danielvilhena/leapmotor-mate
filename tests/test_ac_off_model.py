"""A/C full-OFF is model-specific (#67). The T03 accepts `ac_switch operate=off` from the cloud but
the car ignores it, so it needs the dedicated `ac_off` action (what markoceri's own T03 app uses).
B10/C10/B05 must stay EXACTLY on `ac_switch operate=off` — the confirmed, working path we don't touch.
CI-safe: _session and _session_car_type are stubbed, no network."""
import command_client as cc


class _FakeApi:
    def __init__(self):
        self.calls = []

    def ac_off(self, vin):
        self.calls.append(("ac_off", vin, None))
        return {"code": 0}

    def ac_switch(self, vin, *, params=None):
        self.calls.append(("ac_switch", vin, params))
        return {"code": 0}


class _FakeSession:
    def __init__(self):
        self.api = _FakeApi()

    def execute(self, fn):
        fn(self.api, "VIN")
        return True, "ok"


def _stub(monkeypatch, car_type):
    fake = _FakeSession()
    monkeypatch.setattr(cc, "_session", fake)
    monkeypatch.setattr(cc, "_session_car_type", lambda: car_type)
    return fake


def test_t03_uses_dedicated_ac_off(monkeypatch):
    fake = _stub(monkeypatch, "T03")
    cc.ac_off()
    action, vin, params = fake.api.calls[-1]
    assert action == "ac_off"          # dedicated action, not ac_switch
    assert vin == "VIN"
    assert params is None              # no operate payload


def test_b10_stays_on_ac_switch_off(monkeypatch):
    fake = _stub(monkeypatch, "B10")
    cc.ac_off()
    action, _vin, params = fake.api.calls[-1]
    assert action == "ac_switch"       # UNCHANGED — confirmed working path
    assert params == {"operate": "off"}


def test_c10_b05_and_unknown_stay_on_ac_switch_off(monkeypatch):
    # Every non-T03 model (including an unknown/empty car_type) must keep the B10/C10 behaviour.
    for model in ("C10", "B05", "", "b10"):   # note: matching is on the exact upper "T03" only
        fake = _stub(monkeypatch, model)
        cc.ac_off()
        action, _vin, params = fake.api.calls[-1]
        assert action == "ac_switch", f"{model!r} must stay on ac_switch, got {action}"
        assert params == {"operate": "off"}, f"{model!r} lost its operate=off payload"
