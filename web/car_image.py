"""Live Overview car image.

Composes the per-vehicle car-picture *layer package* to reflect the current state (charge cable,
charging animation, trunk) instead of serving a single static render — mirrors the official app.
The model + colour are baked into the downloaded package, so this works for any car. Falls back to
the package's static image on any problem, so the Overview never breaks.

Reflects the charge cable (+ charging animation), the tailgate, all 4 doors, and the 2 left-side
windows (the only ones drawn in the 3/4 view) — driven by the body state in `positions`. The right
windows and the front hood have no layer/signal, so they're never shown (same as the official app).
"""
from __future__ import annotations

from types import SimpleNamespace as NS

# Decoding the ~39-layer package is the costly part, so keep the parsed package in memory and
# re-decode only when the bytes change (i.e. the car/colour package was re-downloaded).
_parsed: dict = {"key": None, "pkg": None}


def _package(package_bytes: bytes):
    from leapmotor_api.image import CarImagePackage
    key = len(package_bytes)
    if _parsed["key"] != key or _parsed["pkg"] is None:
        _parsed["pkg"] = CarImagePackage.from_zip(package_bytes)
        _parsed["key"] = key
    return _parsed["pkg"]


def _status_obj(status: dict):
    """A minimal duck-typed VehicleStatus — only the fields `leapmotor_api.image` reads, mapped from
    Mate's `get_latest_status()`: the **4 doors** + tailgate, the **2 left-side windows** (the only
    ones the 3/4 render draws), and the charge cable (plug/charging). Old rows (pre-migration) report
    None for the per-door/window keys → treated as closed."""
    s = status or {}
    charging = bool(s.get("charging"))
    plugged = bool(s.get("plug_connected")) or charging

    def _open(key):
        return 1 if s.get(key) else 0

    return NS(
        doors=NS(
            lbcm_driver_door_status=_open("door_driver_open"),        # driver  = left front
            rbcm_driver_door_status=_open("door_passenger_open"),     # passenger = right front
            lbcm_left_rear_door_status=_open("door_rear_left_open"),
            rbcm_right_rear_door_status=_open("door_rear_right_open"),
            bbcm_back_door_status=_open("trunk_open"),                # tailgate
        ),
        windows=NS(
            # The compositor draws the closed-window glass when percent == 0. Drop it when the open door
            # would overlap it, otherwise the glass floats over the swung-out door. The open front-left
            # (driver) door overlaps BOTH left windows, so it suppresses both glasses; the rear-left door
            # suppresses only its own (graphic fix).
            left_front_window_percent=30 if (s.get("window_fl_open") or s.get("door_driver_open")) else 0,
            left_rear_window_percent=30 if (s.get("window_rl_open") or s.get("door_rear_left_open")
                                            or s.get("door_driver_open")) else 0,
        ),
        is_plugged=plugged,
        is_charging=charging,
        battery=NS(is_charging=charging),
    )


def compose(package_bytes: bytes, status: dict) -> tuple[bytes, str]:
    """Return ``(image_bytes, media_type)`` reflecting the live state — an animated WebP while
    charging, a static PNG otherwise. Raises on any problem so the caller can fall back."""
    return _package(package_bytes).compose_animated(_status_obj(status))


def static_image(package_bytes: bytes) -> bytes | None:
    """The package's pre-rendered static car PNG — the fallback / legacy behaviour."""
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as z:
            return z.read("android/xxhdpi/carpic_for_tripsum.png")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def clear_cache() -> None:
    _parsed["key"] = None
    _parsed["pkg"] = None
