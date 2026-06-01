"""
Leapmotor API client wrapper with B10 endpoint fix.
leapmotor-api v0.1.4 bug: B10 uses /status/get/b10 but the international
backend only exposes /status/get/c10 for this model.
"""
import json
import logging
import types
from dataclasses import dataclass
from urllib.parse import quote

from leapmotor_api import LeapmotorApiClient
from leapmotor_api.client import build_signed_headers

log = logging.getLogger(__name__)


@dataclass
class VehicleData:
    vin: str
    timestamp_ms: int
    soc: float
    range_km: float
    odometer_km: float
    speed_kmh: float
    gear: str            # P R N D
    vehicle_state: str   # parked driving
    charging_status: int
    charge_power_kw: float
    latitude: float
    longitude: float
    outside_temp: float
    inside_temp: float
    climate_target_temp: float
    battery_min_temp: float
    is_locked: bool
    climate_on: bool
    climate_cooling: bool     # quick-cool active (signal 2669 == 2)
    climate_heating: bool     # quick-heat active (signal 2681 == 2)
    climate_defrost: bool     # windshield defrost active (signal 1945 == 2)
    trunk_open: bool
    windows_open: bool
    sunshade_open: bool
    any_door_open: bool       # driver/passenger/rear doors or trunk
    plug_connected: bool      # cable inserted (signal 1149)
    remaining_charge_min: int # minutes to full (signal 1200), 0 when not charging
    charge_voltage_v: float   # charging voltage (signal 1177)
    charge_current_a: float   # charging current (signal 1178)

    def fingerprint(self) -> tuple:
        """Compact snapshot of signals that indicate car activity."""
        return (
            self.is_locked,
            round(self.soc),           # 1% granularity avoids noise
            round(self.inside_temp),   # 1°C granularity
            self.any_door_open,
            self.charging_status,
            self.plug_connected,
        )


def _b10_patched_get_vehicle_raw_status(self, vehicle):
    """Replacement for LeapmotorApiClient._get_vehicle_raw_status with B10 fix."""
    car_type_path = "c10" if vehicle.car_type.upper() == "B10" else vehicle.car_type.lower()
    headers = build_signed_headers(
        sign_key=self.sign_key,
        device_id=self.device_id,
        vin=vehicle.vin,
        language=self.language,
    )
    headers.update(self._auth_headers(content_type="application/x-www-form-urlencoded"))
    response = self._post(
        path=f"/carownerservice/oversea/vehicle/v1/status/get/{car_type_path}",
        headers=headers,
        data=f"vin={quote(vehicle.vin, safe='')}",
        cert=self.account_cert,
    )
    return self._parse_api_body(response["status_code"], response["body"], "vehicle status")


class LeapmotorMateClient:
    def __init__(self, username: str, password: str, pin: str, cert_path: str, key_path: str,
                 device_id: str | None = None):
        self._api = LeapmotorApiClient(
            username=username,
            password=password,
            operation_password=pin,
            app_cert_path=cert_path,
            app_key_path=key_path,
            language="en-US",
            device_id=device_id,
        )
        self._api._get_vehicle_raw_status = types.MethodType(
            _b10_patched_get_vehicle_raw_status, self._api
        )
        import session_share
        session_share.install(self._api)   # share ONE token with the web (avoid mutual eviction)
        self._vehicle = None

    def login(self):
        self._api.login()
        vehicles = self._api.get_vehicle_list()
        if not vehicles:
            raise RuntimeError("No vehicles found on this account")
        self._vehicle = vehicles[0]
        log.info("Authenticated — VIN: %s  model: %s", self._vehicle.vin, self._vehicle.car_type)

    def get_status(self) -> VehicleData:
        raw = self._api.get_vehicle_raw_status(self._vehicle)
        return _parse_signal(self._vehicle.vin, raw["data"]["signal"])

    def close(self):
        self._api.close()


_GEAR_MAP = {0: "P", 1: "R", 2: "N", 3: "D"}


def _sf(sig: dict, k: str):
    v = sig.get(k)
    try:    return float(v) if v is not None else None
    except (TypeError, ValueError): return None


def _si(sig: dict, k: str):
    v = sig.get(k)
    try:    return int(v) if v is not None else None
    except (TypeError, ValueError): return None


# Below this magnitude the charge current is just plugged-idle / sensor noise.
_CHARGE_CURRENT_MIN_A = 3.0


def _charge_power_kw(sig: dict) -> float:
    """Charge/regen power from current (1178) × voltage (1177). Signal 49 is NOT a
    power — in the Leapmotor app it's the left-mirror-heating flag. Magnitude only;
    the recorder decides charge vs regen from the current sign."""
    current = _sf(sig, "1178")
    voltage = _sf(sig, "1177")
    if current is None or voltage is None or abs(current) < _CHARGE_CURRENT_MIN_A:
        return 0.0
    return round(abs(current * voltage) / 1000.0, 3)


def _is_charging(sig: dict) -> bool:
    """Whether the car is actually charging. Charging only happens while PARKED, so the
    car must be stationary (gear P, speed ~0); plus the cable plugged in (1149) AND a
    meaningful charge current (1178). The motion gate is essential: during regen braking
    the pack current is strongly negative (same sign as charging) AND 1149 reads 1
    spuriously, so without it driving is mistaken for charging — fragmenting trips and
    creating phantom charge sessions. Signal 1939 (AC fan mode) is NOT used."""
    if _si(sig, "1010") not in (None, 0):   # gear R/N/D → moving, cannot be charging
        return False
    if (_sf(sig, "1319") or 0) > 2.0:       # speed > 2 km/h → moving (gear signal may lag)
        return False
    if _si(sig, "1149") in (None, 0):   # cable not connected → cannot be charging
        return False
    current   = _sf(sig, "1178")
    remaining = _si(sig, "1200")
    power     = _charge_power_kw(sig)

    if current is not None:
        if abs(current) < _CHARGE_CURRENT_MIN_A:   # resting/plugged-idle → not charging
            return False
        return remaining is not None or power >= 1.0
    if power >= 1.0:
        return remaining is not None
    return _si(sig, "1149") == 2                    # fallback: connection status "charging"


def _parse_signal(vin: str, sig: dict) -> VehicleData:
    drive_status = int(sig.get("1941") or 0)
    vehicle_state_code = int(sig.get("1944") or 1)

    if drive_status in (1, 2, 4, 7) or vehicle_state_code in (1, 2, 4):
        vehicle_state = "parked"
    elif drive_status in (3, 5) or vehicle_state_code == 5:
        vehicle_state = "driving"
    else:
        vehicle_state = "parked"

    return VehicleData(
        vin=vin,
        timestamp_ms=int(sig.get("sts") or sig.get("1") or 0),
        soc=float(sig.get("100003") or sig.get("1204") or 0),
        range_km=float(sig.get("3260") or 0),
        odometer_km=float(sig.get("1318") or 0),
        speed_kmh=float(sig.get("1319") or 0),
        gear=_GEAR_MAP.get(int(sig.get("1010") or 0), "P"),
        vehicle_state=vehicle_state,
        charging_status=1 if _is_charging(sig) else 0,
        charge_power_kw=_charge_power_kw(sig),
        latitude=float(sig.get("3725") or sig.get("2190") or 0),
        longitude=float(sig.get("3724") or sig.get("2191") or 0),
        outside_temp=float(sig.get("2101") or 0),
        inside_temp=float(sig.get("1349") or 0),
        climate_target_temp=float(sig.get("2183") or 0),
        battery_min_temp=float(sig.get("1182") or 0),
        is_locked=int(sig.get("1298") or 0) == 1,
        climate_on=int(sig.get("1938") or 0) == 1,
        climate_cooling=int(sig.get("2669") or 0) == 2,
        climate_heating=int(sig.get("2681") or 0) == 2,
        climate_defrost=int(sig.get("1945") or 0) == 2,
        trunk_open=int(sig.get("1281") or 0) != 0,
        windows_open=any(int(sig.get(k) or 0) != 0 for k in ("1693", "1694", "1695", "1696")),
        sunshade_open=int(sig.get("1724") or 0) != 0,
        any_door_open=any(
            int(sig.get(k) or 0) != 0
            for k in ("1277", "1278", "1279", "1280", "1281")
        ),
        plug_connected=int(sig.get("1149") or 0) != 0,
        remaining_charge_min=int(sig.get("1200") or 0),
        charge_voltage_v=float(sig.get("1177") or 0),
        charge_current_a=float(sig.get("1178") or 0),
    )
