"""Live Overview car image (car_image.py).

Mate composes the per-vehicle layer package to reflect the live state (charge cable / charging
animation / trunk) instead of a static render. These cover the Mate-specific bridge
(`get_latest_status()` dict → the duck-typed VehicleStatus the compositor reads), the static
fallback, and the PNG-vs-animated-WebP selection."""
import io
import zipfile

import pytest

import car_image


# ── the status bridge (pure, no Pillow needed) ──────────────────────────────────
def test_bridge_plug_and_charging():
    st = car_image._status_obj({"plug_connected": 1, "charging": 0})
    assert st.is_plugged is True and st.is_charging is False

    sc = car_image._status_obj({"charging": 1})
    assert sc.is_charging is True
    assert sc.is_plugged is True            # charging implies the cable is in
    assert sc.battery.is_charging is True


def test_bridge_trunk_maps_to_tailgate():
    assert car_image._status_obj({"trunk_open": 1}).doors.bbcm_back_door_status == 1
    assert car_image._status_obj({}).doors.bbcm_back_door_status == 0


def test_bridge_maps_four_doors():
    # Mate's per-door keys → the library's door fields (driver = left front, passenger = right front).
    d = car_image._status_obj({
        "door_driver_open": 1, "door_passenger_open": 0,
        "door_rear_left_open": 1, "door_rear_right_open": 0,
    }).doors
    assert d.lbcm_driver_door_status == 1
    assert d.rbcm_driver_door_status == 0
    assert d.lbcm_left_rear_door_status == 1
    assert d.rbcm_right_rear_door_status == 0


def test_bridge_maps_left_windows():
    # Only the 2 left windows are drawn; open → non-zero percent (glass removed), closed → 0 (glass).
    w = car_image._status_obj({"window_fl_open": 1, "window_rl_open": 0}).windows
    assert w.left_front_window_percent != 0
    assert w.left_rear_window_percent == 0


def test_open_left_door_suppresses_window_glass():
    # The open front-left (driver) door overlaps BOTH left windows → suppress both glasses.
    wf = car_image._status_obj({"door_driver_open": 1}).windows
    assert wf.left_front_window_percent != 0
    assert wf.left_rear_window_percent != 0
    # The rear-left door suppresses only its own glass (front unaffected).
    wr = car_image._status_obj({"door_rear_left_open": 1}).windows
    assert wr.left_front_window_percent == 0
    assert wr.left_rear_window_percent != 0


def test_bridge_all_closed_when_empty():
    st = car_image._status_obj({})
    d = st.doors
    assert (d.lbcm_driver_door_status, d.rbcm_driver_door_status, d.lbcm_left_rear_door_status,
            d.rbcm_right_rear_door_status, d.bbcm_back_door_status) == (0, 0, 0, 0, 0)
    assert (st.windows.left_front_window_percent, st.windows.left_rear_window_percent) == (0, 0)


def test_bridge_handles_none_and_empty():
    for s in (None, {}):
        st = car_image._status_obj(s)
        assert st.is_plugged is False and st.is_charging is False
        assert st.doors.bbcm_back_door_status == 0


def test_static_image_on_bad_bytes_returns_none():
    assert car_image.static_image(b"not a zip") is None


# ── compose end-to-end (needs Pillow — present via leapmotor-api[image]) ─────────
def _tiny_package() -> bytes:
    """A minimal layer package (transparent stand-ins) so compose() can run without the real car."""
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    names = [
        "carpic_body.png", "carpic_hood_close.png",
        "carpic_rightbehind_close.png", "carpic_rightfront_close.png",
        "carpic_leftbehind_close.png", "carpic_leftfront_close.png",
        "carpic_leftfront_window_close.png", "carpic_leftbehind_window_close.png",
        "carpic_charge_open.png", "carpic_charge1.png", "carpic_for_tripsum.png",
    ] + [f"carpic_charge{i}.png" for i in range(2, 16)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            b = io.BytesIO()
            Image.new("RGBA", (12, 8), (0, 0, 0, 0)).save(b, format="PNG")
            z.writestr(f"android/xxhdpi/{n}", b.getvalue())
    return buf.getvalue()


def test_compose_static_png_when_idle():
    pkg = _tiny_package()
    car_image.clear_cache()
    body, mime = car_image.compose(pkg, {})
    assert mime == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"


def test_compose_animated_webp_when_charging():
    pkg = _tiny_package()
    car_image.clear_cache()
    body, mime = car_image.compose(pkg, {"charging": 1})
    assert mime == "image/webp" and body[:4] == b"RIFF"


def test_static_image_extracts_tripsum_from_real_shape():
    pkg = _tiny_package()
    assert car_image.static_image(pkg) is not None
