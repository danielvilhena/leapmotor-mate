"""Post-command verification loop — GitHub #34 (command tiles stayed stale).

The old _post_command_refresh took ONE cloud sample ~3s after a command; the
Leapmotor cloud usually hadn't ingested the new state yet, so a command that
actually worked got "un-confirmed" — the optimistic overlay was cleared AND the
stale sample saved as the newest row, poisoning every later refetch. The fix
polls until the cloud confirms (or a deadline), never persisting an unconfirmed
sample mid-wait, with an epoch guard so a newer command supersedes an older
verification.

These need web.main (fastapi); the minimal CI env skips this module cleanly.
"""
import pytest

pytest.importorskip("fastapi", reason="web.main needs fastapi (absent in the minimal CI test env)")
import main


class _Clock:
    """Virtual clock: sleep advances time so the 30s deadline is reached instantly."""
    def __init__(self, t=1000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _patch(monkeypatch, signals_fn):
    clock = _Clock()
    monkeypatch.setattr(main.time, "time", clock.time)
    monkeypatch.setattr(main.time, "sleep", clock.sleep)
    calls = {"save": [], "clear": 0, "extend": 0}
    monkeypatch.setattr(main.command_client, "get_fresh_signals", signals_fn)
    monkeypatch.setattr(main.db_reader, "save_fresh_signals", lambda s: calls["save"].append(s))
    monkeypatch.setattr(main.db_reader, "clear_optimistic_status",
                        lambda: calls.__setitem__("clear", calls["clear"] + 1))
    monkeypatch.setattr(main.db_reader, "extend_optimistic_status",
                        lambda: calls.__setitem__("extend", calls["extend"] + 1))
    return clock, calls


def test_confirmed_command_saves_immediately(monkeypatch):
    _, calls = _patch(monkeypatch, lambda: {"1298": 1})        # car reports locked
    main._command_epoch = 5
    main._post_command_refresh({"is_locked": 1}, epoch=5, delay=3, deadline_s=30)
    assert len(calls["save"]) == 1 and calls["clear"] == 0


def test_stale_sample_is_not_persisted_before_deadline(monkeypatch):
    """The core regression: a still-stale cloud sample mid-wait must not be saved
    nor clear the overlay until the deadline — only reality at the end."""
    polls = []
    def _stale():
        polls.append(1)
        return {"1298": 0}                                     # still unlocked (stale)
    _, calls = _patch(monkeypatch, _stale)
    main._command_epoch = 1
    main._post_command_refresh({"is_locked": 1}, epoch=1, delay=3, deadline_s=30)
    assert calls["clear"] == 1                                 # cleared exactly once, at the deadline
    assert len(calls["save"]) == 1                            # only the final reality save
    assert calls["extend"] >= 1                               # overlay kept alive while waiting
    assert len(polls) > 1                                      # it really retried, not one-shot


def test_newer_command_supersedes_older_verification(monkeypatch):
    """Command #1's verification must stand down (touch nothing) once command #2 has
    bumped the epoch — so its timeout can't clear command #2's overlay."""
    _, calls = _patch(monkeypatch, lambda: {"1298": 0})
    main._command_epoch = 7                                    # a newer command already ran
    main._post_command_refresh({"is_locked": 1}, epoch=6, delay=3, deadline_s=30)
    assert calls["save"] == [] and calls["clear"] == 0


def test_climate_waits_for_the_real_flip(monkeypatch):
    """Climate has no optimistic overlay; verification now waits for the actual
    signal to flip instead of blind-saving whatever the cloud had at 12s."""
    state = {"n": 0}
    def _signals():
        state["n"] += 1
        return {"1938": 0} if state["n"] < 3 else {"1938": 1}  # AC on by the 3rd poll
    _, calls = _patch(monkeypatch, _signals)
    main._command_epoch = 2
    main._post_command_refresh({"climate_on": 1}, epoch=2, delay=12, deadline_s=30)
    assert len(calls["save"]) == 1 and calls["clear"] == 0
    assert state["n"] == 3                                     # waited through 2 stale polls


def test_no_signals_does_not_crash_or_save(monkeypatch):
    """A cloud read that returns nothing must not save garbage; it clears at the
    deadline so the UI falls back to the last real row."""
    _, calls = _patch(monkeypatch, lambda: None)
    main._command_epoch = 3
    main._post_command_refresh({"is_locked": 1}, epoch=3, delay=3, deadline_s=30)
    assert calls["save"] == []                                 # nothing to save
    assert calls["clear"] == 1                                 # overlay dropped at deadline
