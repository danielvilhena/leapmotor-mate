"""Command path recovers a token expiry by REFRESHING the token (cheap, keeps the session) before
ever doing a full re-login — a full login() evicts the user's official-app session on a shared account.
Non-token auth errors (e.g. 'verification', vanished cert) still fall back to the full re-login."""
import types

import pytest

pytest.importorskip("leapmotor_api", reason="command_client imports leapmotor_api")
import command_client as cc


class _FakeAPI:
    def __init__(self):
        self.token = "T1"
        self.refresh_token = "R1"
        self.refreshed = 0
        self.closed = 0

    def token_refresh(self):
        self.refreshed += 1
        self.token = "T2"

    def close(self):
        self.closed += 1


def _session_with(monkeypatch, api):
    sess = cc.LeapmotorSession()
    veh = types.SimpleNamespace(vin="VIN123")

    def fake_connect():            # inject the fake api+vehicle instead of a real login
        if sess._api is None:
            sess._api = api
            sess._vehicle = veh

    monkeypatch.setattr(sess, "_connect", fake_connect)
    monkeypatch.setattr(cc.time, "sleep", lambda *_a, **_k: None)
    return sess


def test_token_expiry_refreshes_before_relogin(monkeypatch):
    api = _FakeAPI()
    sess = _session_with(monkeypatch, api)
    n = {"c": 0}

    def action(_api, _vin):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("access token expired / unauthorized")

    ok, _ = sess.execute(action)
    assert ok is True
    assert api.refreshed == 1      # refreshed the token...
    assert api.closed == 0         # ...and never reset → no full re-login (no app eviction)


def test_non_token_auth_error_falls_back_to_relogin(monkeypatch):
    api = _FakeAPI()
    sess = _session_with(monkeypatch, api)
    n = {"c": 0}

    def action(_api, _vin):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("verification failed")   # not a token expiry → not refreshable

    ok, _ = sess.execute(action)
    assert ok is True
    assert api.refreshed == 0      # a 'verification' error must NOT trigger a token refresh
    assert api.closed == 1         # it went through the reset + full re-login path instead


def test_refresh_attempted_once_then_relogin(monkeypatch):
    # token error that persists even after a successful refresh → one refresh, then re-login, then give up
    api = _FakeAPI()
    sess = _session_with(monkeypatch, api)

    def action(_api, _vin):
        raise RuntimeError("token invalid")

    ok, _ = sess.execute(action)
    assert ok is False
    assert api.refreshed == 1      # refresh tried exactly once
    assert api.closed >= 1         # then fell back to the full re-login reset
