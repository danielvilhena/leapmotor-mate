"""Wallbox picker (#wallbox-config): each role's dropdown offers only entities whose unit fits,
so a kWh energy sensor can't be hand-mapped to the kW power role — which would corrupt the
power/cost data the DB derives. The saved choice is never hidden; status/speed are unfiltered;
advanced 'Show all' bypasses it. Pure function → CI-safe."""
import ha_client as H


def _e(eid, unit="", dclass=""):
    return {"entity_id": eid, "name": eid, "unit": unit, "device_class": dclass}


POWER_KW = _e("sensor.wb_power", "kW", "power")
POWER_W  = _e("sensor.wb_power_w", "W", "power")
ENERGY   = _e("sensor.wb_energy", "kWh", "energy")
CURRENT  = _e("number.wb_max_current", "A")
SPEED    = _e("sensor.wb_speed", "km/h")
STATUS   = _e("sensor.wb_status", "")
ALL = [POWER_KW, POWER_W, ENERGY, CURRENT, SPEED, STATUS]


def _ids(role, entities=ALL, selected=None):
    return [e["entity_id"] for e in H.entities_for_role(role, entities, selected)]


def test_power_role_excludes_kwh():
    out = _ids("power")
    assert "sensor.wb_power" in out and "sensor.wb_power_w" in out   # W and kW both fit
    assert "sensor.wb_energy" not in out                            # kWh must NOT be a power option


def test_energy_role_only_energy_units():
    assert _ids("energy") == ["sensor.wb_energy"]


def test_non_critical_roles_are_unfiltered():
    # status (text), speed (km/h), max_power and max_current all vary by wallbox and don't feed
    # the energy/cost maths → no narrowing, so a real sensor is never hidden.
    for role in ("status", "speed", "max_power", "max_current"):
        assert len(_ids(role)) == len(ALL), role


def test_max_power_in_amps_is_not_hidden():
    # V2C/Pulsar report "max available power" as a current (e.g. 32 A), not kW — it must still show.
    amp_max = _e("sensor.wb_max_power_amps", "A")
    assert "sensor.wb_max_power_amps" in [e["entity_id"] for e in H.entities_for_role("max_power", [amp_max])]


def test_saved_choice_is_never_hidden():
    # a previously-saved wrong-unit mapping stays visible so the user can see and fix it
    assert "sensor.wb_energy" in _ids("power", selected="sensor.wb_energy")


def test_device_class_power_without_unit_is_offered():
    odd = _e("sensor.odd_power", "", "power")    # typed power but no unit_of_measurement
    assert _ids("power", entities=[odd]) == ["sensor.odd_power"]
