"""Persistent Leapmotor session — login once, reuse for all commands and status fetches."""
import os
import json
import time
import logging
import threading

from leapmotor_api import LeapmotorApiClient

log = logging.getLogger(__name__)

# App certificate location. The wizard writes the user-provided cert to /data/certs
# (persistent); fall back to the image-bundled CERT_DIR for local dev. Resolved at call
# time so certs uploaded mid-setup are picked up without a restart.
_DATA_CERT_DIR     = os.environ.get("DATA_CERT_DIR", "/data/certs")
_FALLBACK_CERT_DIR = os.environ.get("CERT_DIR", "certs")


def cert_dir() -> str:
    if (os.path.exists(os.path.join(_DATA_CERT_DIR, "app.crt"))
            and os.path.exists(os.path.join(_DATA_CERT_DIR, "app.key"))):
        return _DATA_CERT_DIR
    return _FALLBACK_CERT_DIR


def certs_present() -> bool:
    d = cert_dir()
    return (os.path.exists(os.path.join(d, "app.crt"))
            and os.path.exists(os.path.join(d, "app.key")))


# T03/EU status carries live data as named fields at the top level of `data` instead
# of a numeric-id `signal` sub-dict (C10/B10). Map per leapmotor-api 0.3.1; kept in
# sync with poller/client.py's copy.
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
    """Rebuild a numeric-id `signal` dict from a T03/EU named-field response."""
    if not isinstance(data, dict):
        return None
    sig = {sid: data[name] for sid, name in _SIGNAL_TO_NAMED.items()
           if data.get(name) is not None}
    return sig or None


def _get_credentials() -> tuple[str, str, str]:
    """Read credentials from DB settings, falling back to env vars for dev."""
    try:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        import db_reader as _dr
        user = _dr.get_setting("leapmotor_user") or os.environ.get("LEAPMOTOR_USER", "")
        pwd  = _dr.get_secret("leapmotor_pass") or os.environ.get("LEAPMOTOR_PASS", "")
        pin  = _dr.get_secret("leapmotor_pin")  or os.environ.get("LEAPMOTOR_PIN", "")
    except Exception:
        user = os.environ.get("LEAPMOTOR_USER", "")
        pwd  = os.environ.get("LEAPMOTOR_PASS", "")
        pin  = os.environ.get("LEAPMOTOR_PIN", "")
    return user, pwd, pin


def _make_client() -> LeapmotorApiClient:
    user, pwd, pin = _get_credentials()
    try:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        import db_reader as _dr
        device_id = _dr.get_or_create_device_id()
    except Exception:
        device_id = None
    api = LeapmotorApiClient(
        username=user,
        password=pwd,
        operation_password=pin,
        app_cert_path=os.path.join(cert_dir(), "app.crt"),
        app_key_path=os.path.join(cert_dir(), "app.key"),
        language="en-US",
        device_id=device_id,
    )
    import session_share
    session_share.install(api)   # share ONE token with the poller (avoid mutual eviction)
    return api


class LeapmotorSession:
    """Login once, reuse token for all subsequent API calls."""

    def __init__(self):
        self._api: LeapmotorApiClient | None = None
        self._vehicle = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._api is not None:
            return
        self._api = _make_client()
        self._api.login()
        vehicles = self._api.get_vehicle_list()
        if not vehicles:
            raise RuntimeError("No vehicle found on this account")
        self._vehicle = vehicles[0]
        log.info("Session started — VIN %s  model %s", self._vehicle.vin, self._vehicle.car_type)

    def _is_auth_error(self, err: str) -> bool:
        low = err.lower()
        return any(k in low for k in ("token", "verification", "unauthori", "login"))

    def _is_connection_error(self, err: str) -> bool:
        low = err.lower()
        return any(k in low for k in ("connection aborted", "remotedisconnected", "connectionerror",
                                       "connection reset", "broken pipe", "timed out"))

    def _reset(self):
        try:
            if self._api:
                self._api.close()
        except Exception:
            pass
        self._api = None
        self._vehicle = None
        log.info("Session reset — will re-login on next call")

    def execute(self, action_fn) -> tuple[bool, str]:
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    action_fn(self._api, self._vehicle.vin)
                    return True, "OK"
                except Exception as e:
                    err = str(e)
                    log.error("Command error (attempt %d): %s", attempt + 1, err)
                    if self._is_connection_error(err):
                        # Stale keep-alive connection — reset and retry immediately
                        self._reset()
                        if attempt == 1:
                            return False, err
                        continue
                    if not self._is_auth_error(err):
                        return False, err
                    self._reset()
                    if attempt == 1:
                        return False, err
                    time.sleep(3)  # avoid rate limit before re-login
            return False, "Unknown error"

    def get_fresh_signals(self) -> dict | None:
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    raw = self._api.get_vehicle_raw_status(self._vehicle)
                    data = (raw or {}).get("data") or {}
                    # C10/B10: numeric `signal` dict. T03/EU: named fields at top level.
                    return data.get("signal") or _named_fields_to_signal(data)
                except Exception as e:
                    log.warning("Status fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None

    def get_charge_plan(self) -> dict | None:
        """Return charge plan dict with at least 'charge_limit_percent' key."""
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    raw = self._api.get_vehicle_raw_status(self._vehicle)
                    plan = ((raw.get("data") or {}).get("config") or {}).get("3") or {}
                    return {
                        "charge_limit_percent": plan.get("percent"),
                        "charge_enabled": plan.get("isEnable"),
                        "start_time": plan.get("beginTime"),
                        "end_time": plan.get("endTime"),
                    }
                except Exception as e:
                    log.warning("Charge plan fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None

    def get_car_picture(self) -> bytes | None:
        """Static owner vehicle PNG, extracted from the car-picture package ZIP
        (android/xxhdpi/carpic_for_tripsum.png) — same as the HA integration's
        image.leapmotor_vehicle_picture. Rarely changes → caller should cache."""
        import io, zipfile
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    meta = self._api.get_car_picture(self._vehicle)
                    key = (meta.get("data") or {}).get("key") if isinstance(meta, dict) else None
                    if not key:
                        return None
                    pkg = self._api.download_car_picture_package(picture_key=key)
                    with zipfile.ZipFile(io.BytesIO(pkg)) as z:
                        return z.read("android/xxhdpi/carpic_for_tripsum.png")
                except Exception as e:
                    log.warning("Car picture fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


    def get_energy_breakdown(self) -> dict | None:
        """Last-week energy split (driving / A/C / other), via the library's native
        get_consumption_last_week_breakdown() (0.3.x). Mapped to the dict shape the UI
        already consumes."""
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    b = self._api.get_consumption_last_week_breakdown(self._vehicle)
                    drv, ac, oth = float(b.driver_ec or 0), float(b.ac_ec or 0), float(b.other_ec or 0)
                    total = drv + ac + oth
                    pct = (lambda v: round(v / total * 100, 1)) if total > 0 else (lambda v: 0)
                    return {
                        "driving_kwh": round(drv, 1), "ac_kwh": round(ac, 1), "other_kwh": round(oth, 1),
                        "total_kwh": round(total, 1),
                        "driving_pct": pct(drv), "ac_pct": pct(ac), "other_pct": pct(oth),
                    }
                except Exception as e:
                    log.warning("Energy breakdown fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


    def get_consumption_rank(self) -> dict | None:
        """6-week kWh/100km trend + driver ranking, via the library's native
        get_consumption_weekly_rank() (0.3.x). Mapped to the UI dict shape."""
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    r = self._api.get_consumption_weekly_rank(self._vehicle)
                    weeks = [{"start": w.week_start, "end": w.week_end,
                              "ec": round(float(w.hundred_km_ec or 0), 1)}
                             for w in (r.weekly or [])]
                    rank = getattr(r, "rank", None)
                    return {
                        "rank": rank.rank if rank else None,
                        "current_ec": round(float(rank.hundred_km_ec or 0), 1) if rank else 0,
                        "weeks": weeks,
                    }
                except Exception as e:
                    log.warning("Consumption rank fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


_session = LeapmotorSession()


def get_car_picture() -> bytes | None:
    return _session.get_car_picture()


def get_energy_breakdown() -> dict | None:
    return _session.get_energy_breakdown()


def get_consumption_rank() -> dict | None:
    return _session.get_consumption_rank()


def detect_vehicle(user: str, pwd: str, pin: str) -> dict:
    """Login with provided credentials, return vehicle info. Does NOT save to DB."""
    try:
        api = LeapmotorApiClient(
            username=user,
            password=pwd,
            operation_password=pin,
            app_cert_path=os.path.join(cert_dir(), "app.crt"),
            app_key_path=os.path.join(cert_dir(), "app.key"),
            language="en-US",
        )
        api.login()
        vehicles = api.get_vehicle_list()
        if not vehicles:
            return {"error": "No vehicle found on this account"}
        v = vehicles[0]
        car_type = v.car_type.upper()
        try:
            api.close()
        except Exception:
            pass
        return {"vin": v.vin, "car_type": car_type}
    except Exception as e:
        return {"error": str(e)}


def get_fresh_signals() -> dict | None:
    return _session.get_fresh_signals()

def get_charge_plan() -> dict | None:
    return _session.get_charge_plan()

def set_charge_limit(percent: int):
    return _session.execute(lambda api, vin: api.set_charge_limit(vin, percent))


def lock():              return _session.execute(lambda api, vin: api.lock_vehicle(vin))
def unlock():            return _session.execute(lambda api, vin: api.unlock_vehicle(vin))
def open_trunk():        return _session.execute(lambda api, vin: api.open_trunk(vin))
def close_trunk():       return _session.execute(lambda api, vin: api.close_trunk(vin))
def find_car():          return _session.execute(lambda api, vin: api.find_vehicle(vin))
def ac_on():             return _session.execute(lambda api, vin: api.ac_switch(vin))
# B10 A/C full-OFF: the working payload is ac_switch with operate=off (drives acSwitch
# signal 1938 → 0). Found empirically on-car 2026-06-06 — the lib's ac_off() sends
# operate=close, which on the B10 only flips the HVAC to AUTO (never off). Reported
# upstream (markoceri/leapmotor-api#3).
def ac_off():            return _session.execute(lambda api, vin: api.ac_switch(vin, params={"operate": "off"}))
def quick_cool():        return _session.execute(lambda api, vin: api.quick_cool(vin))
def quick_heat():        return _session.execute(lambda api, vin: api.quick_heat(vin))
def windshield_defrost():return _session.execute(lambda api, vin: api.windshield_defrost(vin))
# Rapid ventilation + temperature: the whole climate is cmd 170 (kerniger payload). "ac_on"
# maps to cmd 170. mode wind = pure ventilation; temperature field sets the target & starts the climate.
def quick_vent():
    body = json.dumps({"circle": "in", "mode": "wind", "operate": "manual", "position": "all",
                       "temperature": "26", "windlevel": "7", "wshld": "0"}, separators=(",", ":"))
    return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="ac_on", cmd_content=body))
def set_climate_temp(temp, inside=None):
    """Set target temp 18–32 °C. Auto mode: cool if cabin warmer than target, heat if cooler, else vent."""
    try:
        t = max(18, min(int(round(float(temp))), 32))
    except (TypeError, ValueError):
        return False, "bad temp"
    mode = "cold" if (inside is not None and inside > t) else ("hot" if (inside is not None and inside < t) else "wind")
    body = json.dumps({"circle": "in", "mode": mode, "operate": "manual", "position": "all",
                       "temperature": str(t), "windlevel": "5", "wshld": "0"}, separators=(",", ":"))
    return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="ac_on", cmd_content=body))
def open_windows():      return _session.execute(lambda api, vin: api.open_windows(vin, value="2"))
def close_windows():     return _session.execute(lambda api, vin: api.close_windows(vin, value="0"))
def battery_preheat():   return _session.execute(lambda api, vin: api.battery_preheat(vin))
def battery_preheat_off():return _session.execute(lambda api, vin: api.battery_preheat_off(vin))
def open_sunshade():     return _session.execute(lambda api, vin: api.open_sunshade(vin))
def close_sunshade():    return _session.execute(lambda api, vin: api.close_sunshade(vin))
# Charge-port cable unlock (right 192). Exposed on the battery card (promised on mate#19).
# NB: a 2026-Q2 staging note flagged this as "accepted but not actuated" on the B10 (same as
# the old A/C-off) — re-verify on-car before any release; pull the button if it's a no-op.
def unlock_charger():    return _session.execute(lambda api, vin: api.unlock_charger(vin))
# Staged but NOT exposed in any UI: live testing showed the B10 ACCEPTS these (cloud returns
# OK) but does NOT actuate them — so they'd be misleading "Done" buttons. Kept ready so they
# can be wired up instantly if a future leapmotor-api / vehicle update makes them work on the
# B10. (No sunroof: the existing open/close "sunshade" already operates the B10's panoramic roof.)
def sentry_on():         return _session.execute(lambda api, vin: api.sentry_mode_on(vin))
def sentry_off():        return _session.execute(lambda api, vin: api.sentry_mode_off(vin))
# Steering / mirror heat — kerniger 0.6.11 payloads (B10-verified): level/value 1=off, 2=on.
def steering_heat_on():  return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="steering_wheel_heat", cmd_content='{"level":"2"}'))
def steering_heat_off(): return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="steering_wheel_heat", cmd_content='{"level":"1"}'))
def mirror_heat_on():    return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="rearview_mirror_heat", cmd_content='{"value":"2"}'))
def mirror_heat_off():   return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="rearview_mirror_heat", cmd_content='{"value":"1"}'))
# Seats — kerniger/leapmotor-ha#41: the WORKING payload (B10+C10) is
# {"position":"driver"|"copilot","level":"0..3"}, NOT the lib's old {"value":"pos,level"}.
# Send it raw via _remote_control. level: 0=off, 1..3 = the three fan/heat speeds.
_SEAT_ACTION = {"heat": "seat_heat", "vent": "seat_ventilation"}
def seat_comfort(func, position, level):
    """func: 'heat'|'vent'; position: 'driver'|'copilot'; level: 0..3 (0=off)."""
    action = _SEAT_ACTION.get(func)
    if not action or position not in ("driver", "copilot"):
        return False, "bad seat args"
    lvl = max(0, min(int(level), 3))
    body = json.dumps({"position": position, "level": str(lvl)}, separators=(",", ":"))
    return _session.execute(lambda api, vin: api._remote_control(vin=vin, action=action, cmd_content=body))
def seat_heat_driver_on():   return seat_comfort("heat", "driver", 3)
def seat_heat_driver_off():  return seat_comfort("heat", "driver", 0)
def seat_vent_driver_on():   return seat_comfort("vent", "driver", 3)
def seat_vent_driver_off():  return seat_comfort("vent", "driver", 0)
def send_destination(name, address, lat, lon):
    """Push a navigation destination to the car (cmd_id 180, no PIN)."""
    return _session.execute(lambda api, vin: api.send_destination(
        vin, address=address, address_name=name,
        latitude=float(lat), longitude=float(lon)))
