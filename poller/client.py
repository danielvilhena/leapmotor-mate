"""
Leapmotor API client wrapper.
Uses leapmotor-api 0.3.1, which natively maps the B10/B11 status path to /c10 and
serves the T03 named-field status, so no endpoint patching is needed here. We still
parse the raw signal dict ourselves (_parse_signal) to stay independent of the
library's typed model and insulated from its enum changes.
"""
import logging
import os
from dataclasses import dataclass

from leapmotor_api import LeapmotorApiClient

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
    # Individual doors / windows / tyres — used by the optional MQTT → HA bridge
    door_driver_open: bool = False
    door_passenger_open: bool = False
    door_rear_left_open: bool = False
    door_rear_right_open: bool = False
    window_fl_open: bool = False
    window_fr_open: bool = False
    window_rl_open: bool = False
    window_rr_open: bool = False
    tire_fl_bar: float = 0.0
    tire_fr_bar: float = 0.0
    tire_rl_bar: float = 0.0
    tire_rr_bar: float = 0.0

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


class EmptyStatusError(Exception):
    """The cloud returned a vehicle status with no live `signal` block — the car is
    asleep / not reporting, or the response was incomplete. Transient: back off and
    retry rather than treating it as a hard failure."""


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
        import session_share
        session_share.install(self._api)   # share ONE token with the web (avoid mutual eviction)
        self._vehicle = None
        self._named_mode_logged = False    # log the T03/EU named-field path once

    def login(self):
        self._api.login()
        vehicles = self._api.get_vehicle_list()
        if not vehicles:
            raise RuntimeError("No vehicles found on this account")
        self._vehicle = vehicles[0]
        log.info("Authenticated — VIN: %s  model: %s  shared: %s",
                 self._vehicle.vin, self._vehicle.car_type,
                 getattr(self._vehicle, "is_shared", False))

    def relogin(self):
        """Force a fresh login to self-heal a broken session. The account TLS cert
        lives in a /tmp temp file; if it vanishes, every request fails forever with
        'Could not find the TLS certificate file'. Dropping the shared-session blob
        and re-logging in re-creates the cert. Also recovers auth/token drops."""
        try:
            import sqlite3
            c = sqlite3.connect(os.environ.get("DB_PATH", "leapmotor_mate.db"), timeout=5)
            c.execute("DELETE FROM settings WHERE key='shared_session'")
            c.commit()
            c.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Could not clear shared session before relogin: %s", e)
        self.login()

    def get_status(self) -> VehicleData:
        raw = self._api.get_vehicle_raw_status(self._vehicle)
        data = (raw or {}).get("data") or {}
        sig = data.get("signal")
        if not sig:
            # T03 / EU responses carry live data as NAMED fields at the top level of
            # `data` (e.g. "soc", "speed", "gearStatus") instead of a numeric-ID
            # `signal` sub-dict like C10/B10. Rebuild the signal dict our parser
            # expects from those named fields (id↔name map per leapmotor-api 0.3.1).
            sig = _named_fields_to_signal(data)
            if sig and not self._named_mode_logged:
                log.info("T03/EU named-field status detected — mapped %d live fields", len(sig))
                self._named_mode_logged = True
        if not sig:
            # Genuinely empty: car asleep / not reporting (or a brief cloud hiccup).
            # Surface a clear, transient error instead of a bare KeyError so the poller
            # can back off cleanly and retry.
            raise EmptyStatusError("vehicle status has no live signals (car asleep or not reporting)")
        return _parse_signal(self._vehicle.vin, sig)

    def close(self):
        self._api.close()


# Numeric signal-id → T03 named-field map (verbatim from leapmotor-api 0.3.1's
# _SIGNAL_TO_NAMED). C10/B10 report these as numeric IDs inside `data["signal"]`;
# the T03 / EU API reports the SAME data as these named fields at the top level of
# `data`. We invert this to rebuild a numeric `signal` dict for the T03 so the shared
# _parse_signal() below works unchanged for every model.
_SIGNAL_TO_NAMED = {
    "47": "acInputSlowCharge", "1204": "soc", "100003": "preciseSoc",
    "1200": "chargeRemainTime", "1178": "batteryCurrent", "1177": "batteryVoltage",
    "1197": "dcInputFastCharge", "1149": "chargeState", "1182": "minBatteryTemp",
    "1186": "batteryThermalRequest", "3736": "chargeCompleted", "48": "healthyChargeEnabled",
    "3737": "chargeScheduleCancelledOnce",
    "3260": "expectedMileage", "2188": "liveRemainingRange", "3257": "maxRange", "3262": "rangeMode",
    "1319": "speed", "1318": "totalMileage", "1010": "gearStatus", "1944": "vehicleState",
    "1480": "parkingBrakeState", "6048": "speedLimit", "6047": "speedLimitUnit",
    "12054": "speedLimitActive",
    "3725": "latitude", "3724": "longitude",
    "1938": "acSwitch", "2183": "acSetting", "2184": "acSettingRight", "1349": "interiorTemp",
    "1943": "recirculationMode", "1945": "windshieldDefrost", "1946": "rearWindowHeating",
    "3713": "climateMode", "2669": "rapidCooling", "2681": "rapidHeating",
    "1939": "acOperateMode", "1941": "acAirVolume",
    "3727": "leftFrontWindowPercent", "3728": "rightFrontWindowPercent",
    "1879": "leftRearWindowPercent", "1880": "rightRearWindowPercent",
    "1693": "driverWindowStatus", "1694": "rightFrontWindowStatus",
    "1695": "leftRearWindowStatus", "1696": "rightRearWindowStatus",
    "1298": "driverDoorLockStatus", "1277": "lbcmDriverDoorStatus", "1278": "rbcmDriverDoorStatus",
    "1279": "lbcmLeftRearDoorStatus", "1280": "rbcmRightRearDoorStatus", "1281": "bbcmBackDoorStatus",
    "2667": "leftFrontTirePressure", "2653": "rightFrontTirePressure",
    "2646": "leftRearTirePressure", "2660": "rightRearTirePressure",
    "2641": "leftFrontTirePressureState", "2648": "rightFrontTirePressureState",
    "2655": "leftRearTirePressureState", "2662": "rightRearTirePressureState",
    "1256": "bcmKeyPositionOn1", "1257": "bcmKeyPositionOn2", "1258": "bcmKeyPositionOn3",
    "2100": "driverSeatHeating", "2101": "driverSeatVentilation",
    "2118": "passengerSeatHeating", "2119": "passengerSeatVentilation",
    "1816": "steeringWheelHeating", "1624": "steeringWheelHeaterMinutes",
    "1255": "vehicleSecurityActive", "3636": "sentryMode",
    "49": "leftMirrorHeating", "50": "rightMirrorHeating", "1724": "roofOpening",
}


def _named_fields_to_signal(data: dict) -> dict | None:
    """Rebuild a numeric-id `signal` dict from a T03/EU response, whose live data is
    carried as named fields at the top level of `data`. Returns None when no known
    named field is present (genuinely empty / car asleep)."""
    if not isinstance(data, dict):
        return None
    sig = {sid: data[name] for sid, name in _SIGNAL_TO_NAMED.items()
           if data.get(name) is not None}
    return sig or None


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
# Default 2.0 A so low-power home charges (and the tail of a charge) are still
# detected; user-tunable from Settings (the poller calls set_charge_current_min
# each cycle). NB: this is unrelated to the regen threshold (recorder.py, -3.0 A).
_CHARGE_CURRENT_MIN_A = 2.0


def set_charge_current_min(amps: float) -> None:
    """Update the charge-detection current floor (A) from the Settings value."""
    global _CHARGE_CURRENT_MIN_A
    if amps and amps > 0:
        _CHARGE_CURRENT_MIN_A = amps


def _charge_power_kw(sig: dict) -> float:
    """Charge/regen power from current (1178) × voltage (1177). Signal 49 is NOT a
    power — in the Leapmotor app it's the left-mirror-heating flag. Magnitude only;
    the recorder decides charge vs regen from the current sign."""
    current = _sf(sig, "1178")
    voltage = _sf(sig, "1177")
    if current is None or voltage is None or abs(current) < _CHARGE_CURRENT_MIN_A:
        return 0.0
    return round(abs(current * voltage) / 1000.0, 3)


def _is_plugged_in(sig: dict) -> bool:
    """Whether the charge cable is physically connected. Signal 47 is the reliable plug
    flag (matches leapmotor-ha _is_plugged_in) — it stays 0 while driving. Signal 1149
    is only a fallback: it reads 1 spuriously during regen at speed, so it must NOT be
    the primary source or driving gets mistaken for a charge session."""
    plug = _si(sig, "47")
    if plug is not None:
        return plug == 1
    return _si(sig, "1149") in (1, 2)


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
        outside_temp=None,   # no ambient-temp signal exists (2101 = driverSeatVentilation)
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
        plug_connected=_is_plugged_in(sig),
        remaining_charge_min=int(sig.get("1200") or 0),
        charge_voltage_v=float(sig.get("1177") or 0),
        charge_current_a=float(sig.get("1178") or 0),
        door_driver_open=int(sig.get("1277") or 0) != 0,
        door_passenger_open=int(sig.get("1278") or 0) != 0,
        door_rear_left_open=int(sig.get("1279") or 0) != 0,
        door_rear_right_open=int(sig.get("1280") or 0) != 0,
        window_fl_open=int(sig.get("1693") or 0) != 0,
        window_fr_open=int(sig.get("1694") or 0) != 0,
        window_rl_open=int(sig.get("1695") or 0) != 0,
        window_rr_open=int(sig.get("1696") or 0) != 0,
        # Tyre signal→wheel mapping per markoceri/leapmotor-api docs (B10 slots are
        # NOT in the obvious order): 2667=LF, 2653=RF, 2646=LR, 2660=RR.
        tire_fl_bar=round(float(sig.get("2667") or 0) / 100.0, 2),
        tire_fr_bar=round(float(sig.get("2653") or 0) / 100.0, 2),
        tire_rl_bar=round(float(sig.get("2646") or 0) / 100.0, 2),
        tire_rr_bar=round(float(sig.get("2660") or 0) / 100.0, 2),
    )
