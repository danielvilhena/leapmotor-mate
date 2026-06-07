"""Render guard for partials/climate_schedule.html (read-only climate-schedule list).

The partial reads control-dict keys (on/mode/start_time/temperature/days) returned by
leapmotor_api.get_climate_schedule, which can't be introspected from this repo (importorskip), and
Jinja resolves a missing key to a silent falsy Undefined — so an upstream key rename would render
every timer as Off / '—' / 'Once' with NO error. This feeds the REAL control dict captured live
from the B10 (2026-06-07) so a rename fails CI instead of rendering blank.

NB the climate schedule is READ-ONLY in Mate: the B10 cloud rejects the SET (cmd 171, code -2)
even with valid data, so Mate only displays these; edit them in the Leapmotor app.

Skipped where jinja2 isn't installed (the CI test env per pytest.ini)."""
import pathlib

import pytest

jinja2 = pytest.importorskip("jinja2", reason="needs jinja2 to render the partial")

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "web" / "templates"

# Exact shape returned by api.get_climate_schedule(vin) on the B10 (live probe 2026-06-07).
B10_SAMPLE = {
    "circle": "in", "days": [], "mode": "nohotcold", "on": "1", "operate": "auto",
    "position": "all", "set_id": "ios_xxx", "start_time": "2026-05-14 06:03:00",
    "temperature": "26", "windlevel": "7", "wshld": "1",
}


def _render(climate_schedule):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tmpl = env.get_template("partials/climate_schedule.html")
    return tmpl.render(climate_schedule=climate_schedule, t=lambda k: k)


def test_real_b10_dict_renders_time_temp_active():
    out = _render([B10_SAMPLE])
    assert "06:03" in out                 # start_time[11:16] — key present & sliced
    assert "26°" in out                   # temperature key present
    assert "sched_active" in out          # on == "1" → active path
    assert "sched_inactive" not in out
    assert "sched_once" in out            # days == [] → once
    assert "sched_climate_none" not in out  # list non-empty → not the empty branch


def test_off_state_and_day_list_mapping():
    sched = {**B10_SAMPLE, "on": "0", "mode": "cold", "days": [1, 3, 5]}
    out = _render([sched])
    assert "sched_inactive" in out
    assert "sched_active" not in out
    assert "dow_mon" in out and "dow_wed" in out and "dow_fri" in out
    assert "sched_once" not in out


def test_empty_schedule_shows_none():
    out = _render([])
    assert "sched_climate_none" in out
